#!/usr/bin/env node

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import {
  aesEcbPaddedSize,
  CDN_BASE_URL,
  DEFAULT_BASE_URL as VENDOR_DEFAULT_BASE_URL,
  DEFAULT_BOT_TYPE,
  MessageItemType,
  MessageState,
  MessageType,
  TypingStatus,
  UploadMediaType,
} from 'wechat-ilink-client';

import {downloadCdn, uploadCdn} from './lib/cdn.mjs';
import {
  assertAccepted,
  classify,
  clientId,
  error,
  requireFields,
  retry,
  splitText,
  withTimeout,
} from './lib/core.mjs';
import {asciiQRCode} from './lib/qr.mjs';
import {StateStore} from './lib/state.mjs';

const PROTOCOL_VERSION = 1;
const BRIDGE_VERSION = '1.0.0';
const DEFAULT_BASE_URL = VENDOR_DEFAULT_BASE_URL;
const DEFAULT_CDN_BASE_URL = CDN_BASE_URL;
const API_TIMEOUT_MS = positiveInt(process.env.NANOCLAW_WEIXIN_TIMEOUT_MS, 30_000);
const LONG_POLL_TIMEOUT_MS = positiveInt(
  process.env.NANOCLAW_WEIXIN_LONG_POLL_TIMEOUT_MS,
  40_000,
);
const ACK_TIMEOUT_MS = positiveInt(process.env.NANOCLAW_WEIXIN_ACK_TIMEOUT_MS, 30_000);
const RETRY_BASE_MS = positiveInt(process.env.NANOCLAW_WEIXIN_RETRY_BASE_MS, 1000);
const MAX_LINE_BYTES = positiveInt(
  process.env.NANOCLAW_WEIXIN_MAX_LINE_BYTES,
  1024 * 1024,
);
const MAX_INBOUND_IMAGE_BYTES = positiveInt(
  process.env.NANOCLAW_WEIXIN_MAX_INBOUND_IMAGE_BYTES,
  20 * 1024 * 1024,
);
const MAX_OUTBOUND_IMAGE_BYTES = positiveInt(
  process.env.NANOCLAW_WEIXIN_MAX_OUTBOUND_IMAGE_BYTES,
  20 * 1024 * 1024,
);
const MAX_QR_REFRESHES = 3;
// Typing tickets are short-lived provider state. Keep them in memory only;
// activity IDs are the sole typing-related values crossing JSONL IPC.
const TYPING_RENEW_MS = positiveInt(
  process.env.NANOCLAW_WEIXIN_TYPING_RENEW_MS,
  10_000,
);
const TYPING_RETRY_MS = positiveInt(
  process.env.NANOCLAW_WEIXIN_TYPING_RETRY_MS,
  1_000,
);
const TYPING_CLEANUP_TIMEOUT_MS = positiveInt(
  process.env.NANOCLAW_WEIXIN_TYPING_CLEANUP_TIMEOUT_MS,
  500,
);
const PROTOCOL_DIAGNOSTICS = new Set([
  'invalid_json_response',
  'response_too_large',
  'invalid_acceptance',
  'invalid_messages',
]);

const stateDir = process.env.NANOCLAW_WEIXIN_STATE_DIR;
const mediaRoot = process.env.NANOCLAW_WEIXIN_MEDIA_ROOT;
if (!stateDir) {
  throw new Error('NANOCLAW_WEIXIN_STATE_DIR is required');
}

const store = new StateStore(stateDir);
let state = store.load();
let stopped = false;
let pollAbort;
let sessionExpiryInProgress = false;
const pending = new Map();
const pendingInbound = new Map();
const typingActivities = new Map();

function positiveInt(raw, fallback) {
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function bridgeError(code, message, options = {}) {
  return Object.assign(new Error(message), {code, ...options});
}

function emit(message) {
  process.stdout.write(`${JSON.stringify({v: PROTOCOL_VERSION, ...message})}\n`);
}

function normalizedError(err) {
  if (err?.structured_error) return err.structured_error;
  if (typeof err?.code === 'string' && !['ETIMEDOUT', 'ECONNRESET'].includes(err.code)) {
    const result = error(err.code, 'operation failed', Boolean(err.retryable), err.providerCode);
    if (err.code === 'protocol_error') {
      result.reason = PROTOCOL_DIAGNOSTICS.has(err.diagnostic)
        ? err.diagnostic
        : 'provider_protocol_error';
    }
    return result;
  }
  return classify(err);
}

function randomWechatUin() {
  return Buffer.from(
    String(crypto.randomBytes(4).readUInt32BE(0)),
    'utf8',
  ).toString('base64');
}

function requestHeaders(account, payload) {
  const headers = {
    Authorization: `Bearer ${account.bot_token}`,
    AuthorizationType: 'ilink_bot_token',
    'Content-Length': String(Buffer.byteLength(payload)),
    'Content-Type': 'application/json',
    'iLink-App-ClientVersion': String(0x00010000),
    'iLink-App-Id': 'bot',
    'X-WECHAT-UIN': randomWechatUin(),
  };
  if (account.route_tag) headers.SKRouteTag = account.route_tag;
  return headers;
}

function baseInfo() {
  return {
    channel_version: BRIDGE_VERSION,
    bot_agent: `NanoClaw/${BRIDGE_VERSION}`,
  };
}

async function readJsonResponse(response) {
  if (!response.ok) {
    throw Object.assign(new Error('provider HTTP failure'), {
      providerCode: response.status,
    });
  }
  try {
    const declared = Number(response.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > MAX_LINE_BYTES) {
      throw bridgeError('protocol_error', 'provider JSON response exceeds limit', {diagnostic: 'response_too_large'});
    }
    const text = await boundedResponseBytes(response, MAX_LINE_BYTES)
      .then(bytes => bytes.toString('utf8'));
    return JSON.parse(text);
  } catch (err) {
    if (err?.code === 'protocol_error') throw err;
    throw bridgeError('protocol_error', 'provider returned invalid JSON', {diagnostic: 'invalid_json_response'});
  }
}

async function boundedResponseBytes(response, maxBytes) {
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw bridgeError('protocol_error', 'provider response exceeds limit', {diagnostic: 'response_too_large'});
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, size);
}

async function transport(endpoint, body, signal, timeoutMs = API_TIMEOUT_MS) {
  const account = state.account;
  if (!account) {
    throw Object.assign(new Error('not logged in'), {providerCode: -14});
  }
  const payload = JSON.stringify({...body, base_info: baseInfo()});
  return withTimeout(async deadlineSignal => {
    const response = await fetch(new URL(endpoint, account.base_url), {
      method: 'POST',
      headers: requestHeaders(account, payload),
      body: payload,
      signal: deadlineSignal,
    });
    return assertAccepted(await readJsonResponse(response));
  }, timeoutMs, signal);
}

function typingKey(accountId, userId) {
  return JSON.stringify([accountId, userId]);
}

function requireTypingParams(params) {
  requireParams(params, ['account_id', 'user_id', 'activity_id']);
  if (
    typeof params.activity_id !== 'string'
    || params.activity_id.length > 128
    || !/^[A-Za-z0-9_-]+$/.test(params.activity_id)
  ) {
    throw bridgeError('invalid_request', 'invalid typing activity');
  }
}

function getTypingRecord(accountId, userId) {
  const key = typingKey(accountId, userId);
  let record = typingActivities.get(key);
  if (!record) {
    record = {
      accountId,
      userId,
      activities: new Set(),
      operation: Promise.resolve(),
      timer: undefined,
      ticket: undefined,
      active: false,
    };
    typingActivities.set(key, record);
  }
  return record;
}

function hasTypingActivities(record) {
  return record.activities.size > 0;
}

function clearTypingTimer(record) {
  if (record.timer !== undefined) {
    clearTimeout(record.timer);
    record.timer = undefined;
  }
}

function enqueueTyping(record, operation) {
  const next = record.operation.catch(() => {}).then(operation);
  record.operation = next;
  return next;
}

async function typingEndpoint(record, status, signal) {
  if (!state.account || record.accountId !== state.account.account_id) {
    throw Object.assign(new Error('account unavailable'), {providerCode: -14});
  }

  const context = state.context_tokens[record.accountId]?.[record.userId]?.token;
  if (!context && (status === TypingStatus.TYPING || !record.ticket)) {
    throw bridgeError('context_missing', 'no context for typing');
  }
  const timeoutMs = status === TypingStatus.CANCEL
    ? Math.min(API_TIMEOUT_MS, TYPING_CLEANUP_TIMEOUT_MS)
    : API_TIMEOUT_MS;

  // The request shapes and status values are the vendor client's
  // getConfig/getTypingTicket + sendTyping contract.  The Bridge keeps the
  // same protocol on its cancellable transport so HTTP and JSON acceptance,
  // route headers, and request aborts remain consistent with sendmessage.
  if (status === TypingStatus.TYPING || !record.ticket) {
    const config = await transport(
      '/ilink/bot/getconfig',
      {
        ilink_user_id: record.userId,
        context_token: context,
      },
      signal,
      timeoutMs,
    );
    requireFields(config, ['typing_ticket']);
    record.ticket = String(config.typing_ticket);
  }
  if (!record.ticket) throw bridgeError('typing_unavailable', 'typing is unavailable');

  // Cancellation is auxiliary work.  Keep its individual wait short so a
  // dead provider cannot hold the Agent or outbound dispatcher open.
  await transport(
    '/ilink/bot/sendtyping',
    {
      ilink_user_id: record.userId,
      typing_ticket: record.ticket,
      status,
    },
    signal,
    timeoutMs,
  );
  return record.ticket;
}

function scheduleTypingRetry(record) {
  clearTypingTimer(record);
  if (stopped || !hasTypingActivities(record)) return;
  record.timer = setTimeout(() => {
    record.timer = undefined;
    if (!hasTypingActivities(record) || stopped) return;
    const operation = enqueueTyping(record, async () => {
      try {
        await typingEndpoint(record, TypingStatus.TYPING);
        record.active = true;
        scheduleTypingRenew(record);
      } catch (err) {
        record.active = false;
        scheduleTypingRetry(record);
        throw err;
      }
    });
    void operation.catch(handleTypingBackgroundError);
  }, TYPING_RETRY_MS);
}

function scheduleTypingRenew(record) {
  clearTypingTimer(record);
  if (stopped || !hasTypingActivities(record)) return;
  record.timer = setTimeout(() => {
    record.timer = undefined;
    if (!hasTypingActivities(record) || stopped) return;
    const operation = enqueueTyping(record, async () => {
      try {
        await typingEndpoint(record, TypingStatus.TYPING);
        record.active = true;
        scheduleTypingRenew(record);
      } catch (err) {
        record.active = false;
        scheduleTypingRetry(record);
        throw err;
      }
    });
    void operation.catch(handleTypingBackgroundError);
  }, TYPING_RENEW_MS);
}

async function startTyping(params, signal) {
  requireTypingParams(params);
  const record = getTypingRecord(params.account_id, params.user_id);
  if (record.activities.has(params.activity_id)) return {active: true};
  record.activities.add(params.activity_id);
  if (record.activities.size > 1) return {active: true};

  return enqueueTyping(record, async () => {
    try {
      await typingEndpoint(record, TypingStatus.TYPING, signal);
      record.active = true;
      scheduleTypingRenew(record);
      return {active: true};
    } catch (err) {
      record.active = false;
      scheduleTypingRetry(record);
      throw err;
    }
  });
}

async function cancelTypingRecord(record) {
  clearTypingTimer(record);
  try {
    if (record.active || record.ticket) {
      await typingEndpoint(record, TypingStatus.CANCEL);
    }
  } finally {
    record.active = false;
    record.ticket = undefined;
  }
}

async function stopTyping(params) {
  requireTypingParams(params);
  const key = typingKey(params.account_id, params.user_id);
  const record = typingActivities.get(key);
  if (!record || !record.activities.delete(params.activity_id)) {
    return {active: false};
  }
  if (hasTypingActivities(record)) return {active: true};

  const operation = enqueueTyping(record, () => cancelTypingRecord(record));
  try {
    await operation;
    return {active: false};
  } finally {
    if (!hasTypingActivities(record) && typingActivities.get(key) === record) {
      typingActivities.delete(key);
    }
  }
}

async function stopAllTyping() {
  const records = [...typingActivities.values()];
  for (const record of records) {
    clearTypingTimer(record);
    record.activities.clear();
  }
  const operations = records.map(record => (
    enqueueTyping(record, () => cancelTypingRecord(record))
  ));
  if (operations.length > 0) {
    let cleanupTimer;
    try {
      await Promise.race([
        Promise.allSettled(operations),
        new Promise(resolve => {
          cleanupTimer = setTimeout(resolve, TYPING_CLEANUP_TIMEOUT_MS);
        }),
      ]);
    } finally {
      clearTimeout(cleanupTimer);
    }
  }
  typingActivities.clear();
}

function handleTypingBackgroundError(err) {
  const failure = normalizedError(err);
  if (failure.code === 'session_expired') {
    void expireSession(failure.provider_code);
  }
}

async function qrFetch(url, options, signal, timeoutMs = API_TIMEOUT_MS) {
  return withTimeout(async deadlineSignal => {
    const response = await fetch(url, {...options, signal: deadlineSignal});
    return readJsonResponse(response);
  }, timeoutMs, signal);
}

function requireParams(params, keys) {
  for (const key of keys) {
    if (params?.[key] === undefined || params?.[key] === null || params[key] === '') {
      throw bridgeError('invalid_request', `missing ${key}`);
    }
  }
}

function controlledImage(filePath) {
  if (!mediaRoot) throw bridgeError('media_invalid', 'media root is unavailable');
  let root;
  let real;
  let original;
  try {
    const stateRoot = fs.realpathSync(stateDir);
    const rootInfo = fs.lstatSync(mediaRoot);
    if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
      throw new Error('invalid media root');
    }
    root = fs.realpathSync(mediaRoot);
    const rootRelative = path.relative(stateRoot, root);
    if (
      rootRelative === ''
      || rootRelative === '..'
      || rootRelative.startsWith(`..${path.sep}`)
      || path.isAbsolute(rootRelative)
    ) throw new Error('invalid media root');
    original = fs.lstatSync(filePath);
    real = fs.realpathSync(filePath);
  } catch {
    throw bridgeError('media_invalid', 'invalid image path');
  }
  const relative = path.relative(root, real);
  if (
    original.isSymbolicLink()
    || relative === ''
    || relative === '..'
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw bridgeError('media_invalid', 'image path is outside the media root');
  }
  const info = fs.statSync(real);
  if (!info.isFile() || info.size <= 0 || info.size > MAX_OUTBOUND_IMAGE_BYTES) {
    throw bridgeError('media_invalid', 'invalid image file');
  }
  const data = fs.readFileSync(real);
  if (imageMime(data) === null) {
    throw bridgeError('media_invalid', 'unsupported image format');
  }
  return data;
}

function imageMime(data) {
  if (data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    return 'image/png';
  }
  if (data.subarray(0, 3).equals(Buffer.from([255, 216, 255]))) return 'image/jpeg';
  if (['GIF87a', 'GIF89a'].includes(data.subarray(0, 6).toString('ascii'))) return 'image/gif';
  if (data.subarray(0, 2).toString('ascii') === 'BM') return 'image/bmp';
  if (
    data.subarray(0, 4).toString('ascii') === 'RIFF'
    && data.subarray(8, 12).toString('ascii') === 'WEBP'
  ) return 'image/webp';
  return null;
}

async function outboundItems(kind, params, signal) {
  if (kind === 'text') {
    return splitText(params.text).map((text, index) => ({
      type: MessageItemType.TEXT,
      text_item: {text},
      client_id: clientId(params.correlation_id, index),
    }));
  }

  requireParams(params, ['file_path']);
  const plaintext = controlledImage(params.file_path);
  const key = crypto.randomBytes(16);
  const keyHex = key.toString('hex');
  const filekey = crypto.randomBytes(16).toString('hex');
  const ciphertextSize = aesEcbPaddedSize(plaintext.length);
  const upload = await transport('/ilink/bot/getuploadurl', {
    filekey,
    media_type: UploadMediaType.IMAGE,
    to_user_id: params.user_id,
    rawsize: plaintext.length,
    rawfilemd5: crypto.createHash('md5').update(plaintext).digest('hex'),
    filesize: ciphertextSize,
    no_need_thumb: true,
    aeskey: keyHex,
  }, signal);
  requireFields(upload, ['upload_param']);

  const cdnBase = state.account.cdn_base_url || DEFAULT_CDN_BASE_URL;
  const downloadParameter = await withTimeout(
    deadlineSignal => uploadCdn(
      cdnBase,
      upload.upload_param,
      filekey,
      plaintext,
      key,
      deadlineSignal,
    ),
    API_TIMEOUT_MS,
    signal,
  );

  const items = [];
  if (params.caption) {
    for (const text of splitText(params.caption)) {
      items.push({type: MessageItemType.TEXT, text_item: {text}});
    }
  }
  items.push({
    type: MessageItemType.IMAGE,
    image_item: {
      media: {
        encrypt_query_param: downloadParameter,
        // iLink's image wire format base64-encodes the 32-byte ASCII hex key,
        // rather than the 16 raw AES bytes. Match Tencent's reference client.
        aes_key: Buffer.from(keyHex, 'ascii').toString('base64'),
        encrypt_type: 1,
      },
      mid_size: ciphertextSize,
    },
  });
  return items.map((item, index) => ({
    ...item,
    client_id: clientId(params.correlation_id, index),
  }));
}

async function send(kind, params, signal) {
  requireParams(params, ['account_id', 'user_id', 'correlation_id']);
  try {
    if (!state.account || params.account_id !== state.account.account_id) {
      throw Object.assign(new Error('account unavailable'), {providerCode: -14});
    }
    const contextToken = state.context_tokens[params.account_id]?.[params.user_id]?.token;
    if (!contextToken) throw bridgeError('context_missing', 'no context for recipient');

    const items = await outboundItems(kind, params, signal);
    let provider;
    for (const item of items) {
      const {client_id: itemClientId, ...messageItem} = item;
      provider = await retry(() => transport('/ilink/bot/sendmessage', {
        msg: {
          from_user_id: '',
          to_user_id: params.user_id,
          client_id: itemClientId,
          message_type: MessageType.BOT,
          message_state: MessageState.FINISH,
          item_list: [messageItem],
          context_token: contextToken,
        },
      }, signal), {signal, baseDelayMs: RETRY_BASE_MS});
    }
    const result = {
      correlation_id: params.correlation_id,
      success: true,
      retryable: false,
      code: 'ok',
      provider_message_id: provider?.message_id || items.at(-1)?.client_id,
      message: 'provider accepted message',
    };
    emit({type: 'event', event: 'delivery_result', data: result});
    return result;
  } catch (err) {
    const failure = normalizedError(err);
    const result = {
      correlation_id: params?.correlation_id,
      success: false,
      ...failure,
    };
    if (params?.correlation_id) {
      emit({type: 'event', event: 'delivery_result', data: result});
    }
    throw Object.assign(err instanceof Error ? err : new Error('send failed'), {
      structured_error: failure,
    });
  }
}

function extractText(message) {
  return (message.item_list || [])
    .filter(item => item?.type === MessageItemType.TEXT && item.text_item?.text != null)
    .map(item => String(item.text_item.text))
    .join('\n');
}

async function inboundImages(message, signal) {
  const images = [];
  for (const item of message.item_list || []) {
    const image = item?.image_item;
    if (!image?.media?.encrypt_query_param) continue;
    const aesKey = image.aeskey
      ? Buffer.from(image.aeskey, 'hex').toString('base64')
      : image.media.aes_key;
    const data = await withTimeout(
      deadlineSignal => downloadCdn(
        state.account.cdn_base_url || DEFAULT_CDN_BASE_URL,
        image.media.encrypt_query_param,
        aesKey,
        deadlineSignal,
        aesEcbPaddedSize(MAX_INBOUND_IMAGE_BYTES),
      ),
      API_TIMEOUT_MS,
      signal,
    );
    if (data.length <= 0 || data.length > MAX_INBOUND_IMAGE_BYTES) {
      emit({
        type: 'event',
        event: 'channel_error',
        data: error('media_invalid', 'inbound image size is invalid', false),
      });
      continue;
    }
    const mimeType = imageMime(data);
    if (!mimeType) {
      emit({
        type: 'event',
        event: 'channel_error',
        data: error('media_invalid', 'unsupported inbound image format', false),
      });
      continue;
    }
    const directory = controlledSubdirectory('inbound');
    const filePath = path.join(directory, `${crypto.randomUUID()}.img`);
    fs.writeFileSync(filePath, data, {mode: 0o600, flag: 'wx'});
    images.push({file_path: filePath, mime_type: mimeType});
  }
  return images;
}

function controlledSubdirectory(name) {
  const stateRoot = fs.realpathSync(stateDir);
  const directory = path.join(stateRoot, name);
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw bridgeError('media_invalid', 'invalid media directory');
  }
  const real = fs.realpathSync(directory);
  const relative = path.relative(stateRoot, real);
  if (
    relative === ''
    || relative === '..'
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw bridgeError('media_invalid', 'media directory escapes state root');
  }
  fs.chmodSync(real, 0o700);
  return real;
}

function deliveryId(accountId, messageId) {
  return `wx1.${Buffer.from(JSON.stringify([accountId, messageId])).toString('base64url')}`;
}

function processedKey(accountId, messageId) {
  return JSON.stringify([accountId, messageId]);
}

function waitBatch(batch, signal) {
  return new Promise(resolve => {
    const finish = () => {
      clearTimeout(batch.timer);
      signal?.removeEventListener('abort', finish);
      batch.resolve = undefined;
      resolve();
    };
    batch.resolve = finish;
    batch.timer = setTimeout(finish, ACK_TIMEOUT_MS);
    signal?.addEventListener('abort', finish, {once: true});
  });
}

function cancellableDelay(milliseconds, signal) {
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

async function expireSession(providerCode) {
  if (sessionExpiryInProgress) return;
  if (!state.account) {
    typingActivities.clear();
    return;
  }
  sessionExpiryInProgress = true;
  pollAbort?.abort();
  try {
    // Try to cancel native typing while the old account/context still exists;
    // failures are intentionally ignored before the durable generation reset.
    await stopAllTyping();
    store.invalidate(state);
    emit({
      type: 'event',
      event: 'session_expired',
      data: {code: providerCode},
    });
  } finally {
    sessionExpiryInProgress = false;
  }
}

async function pollLoop(signal) {
  let retryDelay = 300;
  while (!stopped && state.account && !signal.aborted) {
    let batch;
    try {
      const accountId = state.account.account_id;
      const response = await transport(
        '/ilink/bot/getupdates',
        {get_updates_buf: state.cursor},
        signal,
        LONG_POLL_TIMEOUT_MS,
      );
      retryDelay = 300;
      const cursor = response.get_updates_buf ?? response.cursor ?? state.cursor;
      const rawMessages = response.msgs ?? response.messages ?? [];
      if (!Array.isArray(rawMessages)) {
        throw bridgeError('protocol_error', 'provider messages field is invalid', {diagnostic: 'invalid_messages'});
      }

      const messages = [];
      const fetchedKeys = new Set();
      for (const message of rawMessages) {
        const messageId = message?.message_id ?? message?.msg_id;
        const userId = message?.from_user_id ?? message?.user_id;
        if (!messageId || !userId) {
          emit({
            type: 'event',
            event: 'channel_error',
            data: error('unsupported_message', 'provider message has no stable identity', false),
          });
          continue;
        }
        const key = processedKey(accountId, String(messageId));
        if (!state.processed_message_ids.includes(key) && !fetchedKeys.has(key)) {
          fetchedKeys.add(key);
          messages.push({message, messageId: String(messageId), userId: String(userId), key});
        }
      }

      if (messages.length === 0) {
        if (cursor !== state.cursor) {
          state.cursor = cursor;
          store.save(state);
        }
        // A conforming provider long-polls. Bound a broken/immediate empty
        // response so it cannot turn this loop into a CPU/network spin.
        await cancellableDelay(25, signal);
        continue;
      }

      batch = {keys: [], acked: new Set(), cursor, resolve: undefined, timer: undefined};
      for (const entry of messages) {
        const {message, messageId, userId, key} = entry;
        if (message.context_token) {
          store.rememberContext(
            state,
            accountId,
            userId,
            String(message.context_token),
          );
        }
        const images = await inboundImages(message, signal);
        const id = deliveryId(accountId, messageId);
        batch.keys.push(key);
        pendingInbound.set(id, {key, batch});
        emit({
          type: 'event',
          event: 'inbound_message',
          data: {
            delivery_id: id,
            account_id: accountId,
            user_id: userId,
            message_id: messageId,
            text: extractText(message),
            images,
          },
        });
      }

      await waitBatch(batch, signal);
      if (batch.keys.length > 0 && batch.acked.size === batch.keys.length) {
        state.processed_message_ids = [
          ...state.processed_message_ids,
          ...batch.keys.filter(key => !state.processed_message_ids.includes(key)),
        ].slice(-store.maxProcessed);
        state.cursor = batch.cursor;
        store.save(state);
      }
    } catch (err) {
      const failure = normalizedError(err);
      if (failure.code === 'session_expired') {
        await expireSession(failure.provider_code);
        return;
      }
      if (!stopped && !signal.aborted) {
        emit({type: 'event', event: 'channel_error', data: failure});
        try {
          await cancellableDelay(retryDelay, signal);
        } catch {
          return;
        }
        retryDelay = Math.min(retryDelay * 2, 8_000);
      }
    } finally {
      if (batch) {
        clearTimeout(batch.timer);
        for (const [id, value] of pendingInbound) {
          if (value.batch === batch) pendingInbound.delete(id);
        }
      }
    }
  }
}

function qrHeaders() {
  return {
    AuthorizationType: 'ilink_bot_token',
    'Content-Type': 'application/json',
    'iLink-App-ClientVersion': String(0x00010000),
    'iLink-App-Id': 'bot',
    'X-WECHAT-UIN': randomWechatUin(),
  };
}

async function fetchQRCode(baseUrl, signal) {
  const localTokenList = state.account?.bot_token ? [state.account.bot_token] : [];
  const response = await qrFetch(
    new URL(`/ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(DEFAULT_BOT_TYPE)}`, baseUrl),
    {
      method: 'POST',
      headers: qrHeaders(),
      body: JSON.stringify({local_token_list: localTokenList}),
    },
    signal,
  );
  return requireFields(response, ['qrcode', 'qrcode_img_content']);
}

async function pollQRCode(baseUrl, qrcode, signal, timeoutMs) {
  const response = await qrFetch(
    new URL(`/ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qrcode)}`, baseUrl),
    {method: 'GET', headers: qrHeaders()},
    signal,
    timeoutMs,
  );
  return requireFields(response, ['status']);
}

function emitQRCode(qr) {
  emit({
    type: 'event',
    event: 'qr_code',
    data: {
      qrcode: qr.qrcode,
      image: qr.qrcode_img_content,
      ascii: asciiQRCode(qr.qrcode_img_content),
    },
  });
}

function redirectedBase(currentBase, redirectHost) {
  if (!redirectHost) {
    throw bridgeError('protocol_error', 'redirect status is missing redirect_host');
  }
  if (/^https?:\/\//i.test(redirectHost)) return redirectHost;
  const current = new URL(currentBase);
  return `${current.protocol}//${redirectHost}`;
}

async function login(params, signal) {
  if (state.account && !params.force) {
    emit({
      type: 'event',
      event: 'login_success',
      data: {account_id: state.account.account_id},
    });
    return {account_id: state.account.account_id, resumed: true};
  }

  const recoverableAccount = state.account;
  const initialBase = params.base_url
    || process.env.NANOCLAW_WEIXIN_BASE_URL
    || DEFAULT_BASE_URL;
  const deadline = Date.now() + positiveInt(params.timeout_ms, 480_000);
  let effectiveBase = initialBase;
  let refreshes = 0;
  let qr = await fetchQRCode(initialBase, signal);
  emitQRCode(qr);

  while (Date.now() < deadline) {
    const remaining = Math.max(1, deadline - Date.now());
    let status;
    try {
      status = await pollQRCode(
        effectiveBase,
        qr.qrcode,
        signal,
        Math.min(LONG_POLL_TIMEOUT_MS, remaining),
      );
    } catch (err) {
      const failure = normalizedError(err);
      if (failure.code === 'timeout' || failure.code === 'network_error') {
        emit({type: 'event', event: 'login_status', data: {status: 'wait'}});
        await cancellableDelay(250, signal);
        continue;
      }
      throw err;
    }

    emit({
      type: 'event',
      event: 'login_status',
      data: {status: status.status},
    });

    if (status.status === 'wait' || status.status === 'scaned') {
      await cancellableDelay(250, signal);
      continue;
    }
    if (status.status === 'scaned_but_redirect') {
      effectiveBase = redirectedBase(effectiveBase, status.redirect_host);
      continue;
    }
    if (status.status === 'need_verifycode') {
      throw bridgeError('verification_required', 'login verification is not supported');
    }
    if (status.status === 'binded_redirect') {
      if (recoverableAccount) {
        state.account = recoverableAccount;
        store.save(state);
        emit({
          type: 'event',
          event: 'login_success',
          data: {account_id: recoverableAccount.account_id},
        });
        return {
          account_id: recoverableAccount.account_id,
          resumed: true,
          already_connected: true,
        };
      }
      throw bridgeError(
        'already_bound_unrecoverable',
        'account is bound but no local credentials are available',
      );
    }
    if (status.status === 'expired' || status.status === 'verify_code_blocked') {
      refreshes += 1;
      if (refreshes > MAX_QR_REFRESHES) {
        throw bridgeError(
          status.status === 'verify_code_blocked' ? 'verification_required' : 'qr_expired',
          'QR login refresh limit reached',
        );
      }
      effectiveBase = initialBase;
      qr = await fetchQRCode(initialBase, signal);
      emitQRCode(qr);
      continue;
    }
    if (status.status === 'confirmed') {
      requireFields(status, ['ilink_bot_id', 'bot_token']);
      state.account = {
        account_id: String(status.ilink_bot_id),
        bot_token: String(status.bot_token),
        base_url: String(status.baseurl || effectiveBase),
        route_tag: status.route_tag ? String(status.route_tag) : undefined,
        login_user_id: status.ilink_user_id ? String(status.ilink_user_id) : undefined,
        cdn_base_url: status.cdn_base_url ? String(status.cdn_base_url) : undefined,
      };
      sessionExpiryInProgress = false;
      store.save(state);
      emit({
        type: 'event',
        event: 'login_success',
        data: {account_id: state.account.account_id},
      });
      return {account_id: state.account.account_id, resumed: false};
    }
    throw bridgeError('protocol_error', 'unknown QR login status');
  }
  throw bridgeError('timeout', 'login timed out', {retryable: true});
}

async function request(message) {
  if (
    message?.v !== PROTOCOL_VERSION
    || message?.type !== 'request'
    || typeof message.id !== 'string'
    || !message.id
    || typeof message.method !== 'string'
    || !message.method
  ) {
    throw bridgeError('invalid_request', 'invalid protocol request');
  }
  const params = message.params || {};
  const signal = pending.get(message.id)?.signal;
  switch (message.method) {
    case 'hello':
      return {
        bridge_version: BRIDGE_VERSION,
        protocol_version: PROTOCOL_VERSION,
        capabilities: [
          'login',
          'start',
          'send_text',
          'send_image',
          'typing_start',
          'typing_stop',
          'ack_inbound',
          'stop',
        ],
      };
    case 'login':
      return login(params, signal);
    case 'start':
      stopped = false;
      if (!pollAbort && state.account) {
        pollAbort = new AbortController();
        void pollLoop(pollAbort.signal).finally(() => {
          pollAbort = undefined;
        });
      }
      emit({
        type: 'event',
        event: 'ready',
        data: {account_id: state.account?.account_id ?? null},
      });
      return {started: true};
    case 'send_text':
      return send('text', params, signal);
    case 'send_image':
      return send('image', params, signal);
    case 'typing_start':
      return startTyping(params, signal);
    case 'typing_stop':
      return stopTyping(params);
    case 'ack_inbound': { // Ack is valid only while that delivery is pending.
      requireParams(params, ['delivery_id']);
      const accepted = pendingInbound.get(params.delivery_id);
      if (!accepted) throw bridgeError('invalid_request', 'unknown delivery');
      accepted.batch.acked.add(accepted.key);
      if (accepted.batch.acked.size === accepted.batch.keys.length) {
        accepted.batch.resolve?.();
      }
      return {acked: true};
    }
    case 'stop':
      stopped = true;
      pollAbort?.abort();
      for (const [id, controller] of pending) {
        if (id !== message.id) controller.abort();
      }
      await stopAllTyping();
      emit({type: 'event', event: 'stopped', data: {}});
      return {stopped: true};
    default:
      throw bridgeError('invalid_request', 'unknown method');
  }
}

async function handleLine(line) {
  let message;
  try {
    if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) {
      throw bridgeError('protocol_error', 'JSONL line exceeds limit');
    }
    try {
      message = JSON.parse(line);
    } catch {
      throw bridgeError('protocol_error', 'invalid JSON');
    }
    if (typeof message?.id === 'string' && message.id) {
      if (pending.has(message.id)) {
        throw bridgeError('invalid_request', 'duplicate request id');
      }
      const controller = new AbortController();
      pending.set(message.id, controller);
    }
    const result = await request(message);
    emit({type: 'response', id: message.id, ok: true, result});
  } catch (err) {
    const failure = normalizedError(err);
    if (failure.code === 'session_expired') {
      await expireSession(failure.provider_code);
    }
    emit({
      type: 'response',
      id: typeof message?.id === 'string' ? message.id : null,
      ok: false,
      error: failure,
    });
  } finally {
    if (typeof message?.id === 'string') pending.delete(message.id);
  }
}

const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
emit({type: 'event', event: 'ready', data: {protocol: PROTOCOL_VERSION}});
lines.on('line', line => {
  void handleLine(line);
});
lines.on('close', () => {
  stopped = true;
  pollAbort?.abort();
  for (const controller of pending.values()) controller.abort();
});
