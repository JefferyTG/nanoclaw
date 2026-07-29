import crypto from 'node:crypto';

const MAX_TEXT = 1800;

export const error = (
  code,
  message = 'operation failed',
  retryable = false,
  providerCode,
) => ({
  code,
  message,
  retryable,
  ...(providerCode !== undefined ? {provider_code: providerCode} : {}),
});

export function classify(err) {
  const rawCode = err?.providerCode ?? err?.ret ?? err?.errcode;
  const providerCode = rawCode === undefined || rawCode === null
    ? Number.NaN
    : Number(rawCode);
  if (providerCode === -14) {
    return error('session_expired', 'session expired', false, providerCode);
  }
  if (err?.name === 'TimeoutError' || err?.code === 'ETIMEDOUT') {
    return error('timeout', 'timeout', true);
  }
  if (err?.name === 'AbortError') return error('cancelled', 'cancelled', true);
  if (Number.isFinite(providerCode)) {
    return error(
      'api_rejected',
      'provider rejected request',
      providerCode === -1 || providerCode === 429 || providerCode >= 500,
      providerCode,
    );
  }
  return error('network_error', 'network failure', true);
}

export function clientId(correlationId, part) {
  return crypto
    .createHash('sha256')
    .update(`${correlationId}:${part}`)
    .digest('hex')
    .slice(0, 32);
}

export function splitText(text, max = MAX_TEXT) {
  if (typeof text !== 'string' || !text || !Number.isInteger(max) || max <= 0) {
    throw Object.assign(new Error('text required'), {code: 'invalid_request'});
  }
  const characters = Array.from(text);
  const chunks = [];
  for (let index = 0; index < characters.length; index += max) {
    chunks.push(characters.slice(index, index + max).join(''));
  }
  return chunks;
}

/** HTTP success alone is not an iLink send/update acknowledgement. */
export function assertAccepted(json) {
  if (!json || typeof json !== 'object') {
    throw Object.assign(new Error('provider response is not an object'), {
      code: 'protocol_error',
    });
  }
  if (json.ret === undefined && json.errcode === undefined) {
    throw Object.assign(new Error('provider acceptance field missing'), {
      code: 'protocol_error',
    });
  }
  for (const raw of [json.ret, json.errcode]) {
    if (raw === undefined || raw === null) continue;
    const code = Number(raw);
    if (!Number.isFinite(code)) {
      throw Object.assign(new Error('provider acceptance field is invalid'), {
        code: 'protocol_error',
      });
    }
    if (code !== 0) {
      throw Object.assign(new Error('provider rejected request'), {
        providerCode: code,
      });
    }
  }
  return json;
}

export function requireFields(value, fields) {
  if (
    !value
    || typeof value !== 'object'
    || fields.some(field => value[field] === undefined || value[field] === null || value[field] === '')
  ) {
    throw Object.assign(new Error('provider response missing required field'), {
      code: 'protocol_error',
    });
  }
  return value;
}

function abortableSleep(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(Object.assign(new Error('cancelled'), {name: 'AbortError'}));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(Object.assign(new Error('cancelled'), {name: 'AbortError'}));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener('abort', onAbort, {once: true});
  });
}

export async function retry(
  fn,
  {attempts = 3, baseDelayMs = 1000, sleep, signal} = {},
) {
  let last;
  for (let index = 0; index < attempts; index += 1) {
    if (signal?.aborted) {
      throw Object.assign(new Error('cancelled'), {name: 'AbortError'});
    }
    try {
      return await fn(index);
    } catch (err) {
      last = err;
      if (!classify(err).retryable || index === attempts - 1) throw err;
      const milliseconds = Math.min(baseDelayMs * (2 ** index), 8000);
      if (sleep) await sleep(milliseconds);
      else await abortableSleep(milliseconds, signal);
    }
  }
  throw last;
}

export async function withTimeout(fn, timeoutMs, parent) {
  const controller = new AbortController();
  let timedOut = false;
  const onParentAbort = () => controller.abort();
  if (parent?.aborted) controller.abort();
  else parent?.addEventListener('abort', onParentAbort, {once: true});
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fn(controller.signal);
  } catch (err) {
    if (timedOut) {
      throw Object.assign(new Error('timeout'), {name: 'TimeoutError'});
    }
    throw err;
  } finally {
    clearTimeout(timer);
    parent?.removeEventListener('abort', onParentAbort);
  }
}
