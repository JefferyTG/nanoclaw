import {
  buildCdnDownloadUrl,
  buildCdnUploadUrl,
  decryptAesEcb,
  encryptAesEcb,
  parseAesKey,
} from 'wechat-ilink-client';

// These wrappers deliberately call the exact-pinned community runtime. The
// Bridge adds bounded I/O, response classification and lifecycle semantics.
export const encryptEcb = encryptAesEcb;
export const decryptEcb = decryptAesEcb;
export const parseKey = parseAesKey;

export async function uploadCdn(
  base,
  uploadParam,
  filekey,
  plaintext,
  key,
  signal,
) {
  const response = await fetch(buildCdnUploadUrl({
    cdnBaseUrl: base,
    uploadParam,
    filekey,
  }), {
    method: 'POST',
    headers: {'Content-Type': 'application/octet-stream'},
    body: encryptAesEcb(plaintext, key),
    signal,
  });
  if (!response.ok) {
    throw Object.assign(new Error('CDN upload failed'), {
      providerCode: response.status,
    });
  }
  const parameter = response.headers.get('x-encrypted-param');
  if (!parameter) {
    throw Object.assign(new Error('missing CDN download parameter'), {
      code: 'media_invalid',
    });
  }
  return parameter;
}

async function boundedBody(response, maxBytes) {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    throw Object.assign(new Error('CDN response exceeds limit'), {
      code: 'media_invalid',
    });
  }
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
        throw Object.assign(new Error('CDN response exceeds limit'), {
          code: 'media_invalid',
        });
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, size);
}

export async function downloadCdn(base, query, aesKey, signal, maxBytes = Infinity) {
  const response = await fetch(buildCdnDownloadUrl(query, base), {signal});
  if (!response.ok) {
    throw Object.assign(new Error('CDN download failed'), {
      providerCode: response.status,
    });
  }
  const bytes = await boundedBody(response, maxBytes);
  return aesKey ? decryptAesEcb(bytes, parseAesKey(aesKey)) : bytes;
}
