import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {encryptEcb} from '../lib/cdn.mjs';

import {
  accountState,
  BridgeHarness,
  fakeServer,
  jsonBody,
  jsonResponse,
  temporaryDirectory,
  writePrivateState,
} from './harness.mjs';

// MessageItemType.FILE == 4.  The FILE item carries a CDN media reference with
// a base64 AES key and the original file name as metadata only.
function fileItem(fileName, key, plaintext, query = 'file-download') {
  return {
    type: 4,
    file_item: {
      media: {encrypt_query_param: query, aes_key: key.toString('base64')},
      file_name: fileName,
      md5: 'fake-md5',
      len: String(plaintext.length),
    },
  };
}

function textItem(text) {
  return {type: 1, text_item: {text}};
}

function inboundMessage(id, itemList) {
  return {
    message_id: id,
    from_user_id: 'user',
    context_token: 'context-user',
    item_list: itemList,
  };
}

// Serves the same encrypted bytes for every file CDN request.
async function runInbound(itemList, messageId = 'file-message', options = {}) {
  const {cdnHandler, env = {}} = options;
  const cdn = await fakeServer(
    cdnHandler ||
      (async (request, response) => {
        response.writeHead(200, {'Content-Type': 'application/octet-stream'});
        response.end(Buffer.alloc(0)); // default; tests usually supply a handler
      }),
  );
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    jsonResponse(response, {
      ret: 0,
      get_updates_buf: 'cursor-next',
      msgs:
        body.get_updates_buf === 'cursor-old'
          ? [inboundMessage(messageId, itemList)]
          : [],
    });
  });
  const directory = temporaryDirectory('wx-file-');
  writePrivateState(
    directory,
    accountState(api.baseUrl, {cdn_base_url: `${cdn.baseUrl}/c2c`}),
  );
  const bridge = new BridgeHarness({stateDir: directory, env});
  await bridge.request('start');
  const inbound = await bridge.event('inbound_message');
  return {api, cdn, bridge, inbound};
}

test('FILE item is downloaded to a private controlled temp file and exposed as data.files', async t => {
  const plaintext = Buffer.from('hello NanoClaw file content');
  const key = Buffer.from('0123456789abcdef');
  const encrypted = encryptEcb(plaintext, key);
  const {api, cdn, bridge, inbound} = await runInbound(
    [fileItem('报告.md', key, plaintext)],
    'file-message',
    {
      cdnHandler: async (request, response) => {
        assert.match(request.url, /^\/c2c\/download\?/);
        response.writeHead(200, {'Content-Type': 'application/octet-stream'});
        response.end(encrypted);
      },
    },
  );
  t.after(() => api.close());
  t.after(() => cdn.close());
  t.after(() => bridge.terminate());
  assert.equal(inbound.data.files.length, 1);
  assert.equal(inbound.data.files[0].file_name, '报告.md');
  assert.equal(inbound.data.files[0].size, plaintext.length);
  const filePath = inbound.data.files[0].file_path;
  assert.deepEqual(fs.readFileSync(filePath), plaintext);
  assert.equal(fs.statSync(filePath).mode & 0o777, 0o600);
  // Temp name is a uuid; the original name is metadata only.
  assert.notEqual(path.basename(filePath), '报告.md');
  assert.equal(inbound.data.text, '');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('TEXT item plus FILE item merge: text kept and file reference attached', async t => {
  const plaintext = Buffer.from('mixed message file bytes');
  const key = Buffer.from('0123456789abcdef');
  const encrypted = encryptEcb(plaintext, key);
  const {api, cdn, bridge, inbound} = await runInbound(
    [textItem('看下这个文件'), fileItem('notes.txt', key, plaintext)],
    'mixed-message',
    {
      cdnHandler: async (request, response) => {
        response.writeHead(200, {'Content-Type': 'application/octet-stream'});
        response.end(encrypted);
      },
    },
  );
  t.after(() => api.close());
  t.after(() => cdn.close());
  t.after(() => bridge.terminate());
  assert.equal(inbound.data.text, '看下这个文件');
  assert.equal(inbound.data.files.length, 1);
  assert.equal(inbound.data.files[0].file_name, 'notes.txt');
  assert.equal(inbound.data.files[0].size, plaintext.length);
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('message without FILE items exposes an empty files list (regression)', async t => {
  const {api, cdn, bridge, inbound} = await runInbound(
    [textItem('普通文本')],
    'no-file-message',
  );
  t.after(() => api.close());
  t.after(() => cdn.close());
  t.after(() => bridge.terminate());
  assert.equal(inbound.data.text, '普通文本');
  assert.deepEqual(inbound.data.files, []);
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('FILE larger than the configured cap is skipped with channel_error but still acked', async t => {
  const plaintext = Buffer.alloc(24, 0x61); // 24 bytes plaintext
  const key = Buffer.from('0123456789abcdef');
  const encrypted = encryptEcb(plaintext, key);
  // Cap 17 bytes: ciphertext (padded to 32) fits the CDN bound, but the
  // decrypted payload (24) exceeds the cap and must be dropped.
  const {api, cdn, bridge, inbound} = await runInbound(
    [textItem('附件超大'), fileItem('big.bin', key, plaintext)],
    'oversized-message',
    {
      env: {NANOCLAW_WEIXIN_MAX_INBOUND_FILE_BYTES: '17'},
      cdnHandler: async (request, response) => {
        response.writeHead(200, {'Content-Type': 'application/octet-stream'});
        response.end(encrypted);
      },
    },
  );
  t.after(() => api.close());
  t.after(() => cdn.close());
  t.after(() => bridge.terminate());
  const channelError = await bridge.event(
    'channel_error',
    data => data.code === 'media_invalid',
  );
  assert.equal(channelError.data.message, 'inbound file size is invalid');
  assert.deepEqual(inbound.data.files, []);
  assert.equal(inbound.data.text, '附件超大');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('FILE CDN download failure is reported and skipped without breaking the batch', async t => {
  const key = Buffer.from('0123456789abcdef');
  const {api, cdn, bridge, inbound} = await runInbound(
    [textItem('这个附件可能坏了'), fileItem('broken.bin', key, Buffer.alloc(4))],
    'broken-message',
    {
      cdnHandler: async (request, response) => {
        response.writeHead(500, {'Content-Type': 'application/octet-stream'});
        response.end('boom');
      },
    },
  );
  t.after(() => api.close());
  t.after(() => cdn.close());
  t.after(() => bridge.terminate());
  await bridge.event('channel_error');
  assert.deepEqual(inbound.data.files, []);
  assert.equal(inbound.data.text, '这个附件可能坏了');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});
