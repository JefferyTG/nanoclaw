import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {decryptEcb} from '../lib/cdn.mjs';
import {StateStore} from '../lib/state.mjs';
import {
  accountState,
  BridgeHarness,
  fakeServer,
  jsonBody,
  jsonResponse,
  stableClientId,
  temporaryDirectory,
  writePrivateState,
} from './harness.mjs';

test('text send uses the vendor wire shape and stable IDs across retries', async t => {
  const calls = [];
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    calls.push({headers: request.headers, body});
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-text-');
  const state = accountState(api.baseUrl, {route_tag: 'route-1'});
  state.context_tokens['account-1'] = {user: {token: 'context-secret', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({stateDir: directory});

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await bridge.request('send_text', {
      account_id: 'account-1',
      user_id: 'user',
      text: 'a'.repeat(1801),
      correlation_id: 'stable-correlation',
    });
    assert.equal(response.ok, true);
    assert.equal(response.result.success, true);
  }
  await bridge.stop();

  assert.equal(calls.length, 4);
  for (const call of calls) {
    assert.equal(call.headers.authorization, 'Bearer bot-secret');
    assert.equal(call.headers.authorizationtype, 'ilink_bot_token');
    assert.equal(call.headers['ilink-app-id'], 'bot');
    assert.equal(call.headers['ilink-app-clientversion'], '65536');
    assert.equal(call.headers['skroutetag'], 'route-1');
    assert.match(Buffer.from(call.headers['x-wechat-uin'], 'base64').toString(), /^\d+$/);
    assert.equal(call.body.base_info.channel_version, '1.0.0');
    assert.equal(call.body.base_info.bot_agent, 'NanoClaw/1.0.0');
    assert.equal(call.body.msg.context_token, 'context-secret');
    assert.equal(call.body.msg.item_list[0].client_id, undefined);
  }
  assert.deepEqual(
    calls.slice(0, 2).map(call => call.body.msg.client_id),
    calls.slice(2, 4).map(call => call.body.msg.client_id),
  );
  assert.deepEqual(calls.slice(0, 2).map(call => call.body.msg.client_id), [
    stableClientId('stable-correlation', 0),
    stableClientId('stable-correlation', 1),
  ]);
});

test('send rejection emits a matching failure receipt and never reports success', async t => {
  const api = await fakeServer(async (_request, response) => {
    jsonResponse(response, {ret: 0, errcode: 403});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-reject-');
  const state = accountState(api.baseUrl);
  state.context_tokens['account-1'] = {user: {token: 'context', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({stateDir: directory});
  const response = await bridge.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'hello',
    correlation_id: 'rejected',
  });
  const receipt = await bridge.event(
    'delivery_result',
    data => data.correlation_id === 'rejected',
  );
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'api_rejected');
  assert.equal(response.error.provider_code, 403);
  assert.equal(receipt.data.success, false);
  assert.equal(receipt.data.code, response.error.code);
  await bridge.stop();
});

test('real transport deadline classifies a hanging provider as timeout', async t => {
  const api = await fakeServer(async request => {
    await jsonBody(request);
    // Intentionally leave the response open until the bridge deadline aborts.
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-timeout-');
  const state = accountState(api.baseUrl);
  state.context_tokens['account-1'] = {user: {token: 'context', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({
    stateDir: directory,
    env: {
      NANOCLAW_WEIXIN_TIMEOUT_MS: '20',
      NANOCLAW_WEIXIN_RETRY_BASE_MS: '1',
    },
  });
  const response = await bridge.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'hello',
    correlation_id: 'timeout',
  });
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'timeout');
  assert.equal(response.error.retryable, true);
  await bridge.stop();
});

test('stop cancels an in-flight provider request and returns promptly', async t => {
  let requests = 0;
  const api = await fakeServer(async request => {
    await jsonBody(request);
    requests += 1;
    // Intentionally never finish this response.
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-cancel-');
  const state = accountState(api.baseUrl);
  state.context_tokens['account-1'] = {user: {token: 'context', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({stateDir: directory});
  const send = bridge.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'hello',
    correlation_id: 'cancelled',
  }, 'send');
  while (requests === 0) await new Promise(resolve => setTimeout(resolve, 5));
  const stopped = await bridge.request('stop', {}, 'stop');
  const cancelled = await send;
  assert.equal(stopped.ok, true);
  assert.equal(cancelled.ok, false);
  assert.equal(cancelled.error.code, 'cancelled');
  bridge.child.stdin.end();
  await bridge.exit;
});

test('image upload uses CDN /c2c path, caption ordering, and controlled files', async t => {
  const sent = [];
  let encryptedUpload;
  let uploadRequest;
  const cdn = await fakeServer(async (request, response) => {
    assert.match(request.url, /^\/c2c\/upload\?/);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    encryptedUpload = Buffer.concat(chunks);
    assert.ok(encryptedUpload.length > 0);
    response.writeHead(200, {'x-encrypted-param': 'download-parameter'});
    response.end();
  });
  t.after(() => cdn.close());
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    if (request.url === '/ilink/bot/getuploadurl') {
      uploadRequest = body;
      jsonResponse(response, {ret: 0, upload_param: 'upload-parameter'});
      return;
    }
    if (request.url === '/ilink/bot/sendmessage') {
      sent.push(body.msg);
      jsonResponse(response, {ret: 0});
      return;
    }
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-image-');
  const mediaRoot = path.join(directory, 'outbound');
  fs.mkdirSync(mediaRoot, {mode: 0o700});
  const filePath = path.join(mediaRoot, 'image.png');
  const plaintext = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1]);
  fs.writeFileSync(filePath, plaintext);
  const state = accountState(api.baseUrl, {cdn_base_url: `${cdn.baseUrl}/c2c`});
  state.context_tokens['account-1'] = {user: {token: 'context', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({stateDir: directory, mediaRoot});
  const response = await bridge.request('send_image', {
    account_id: 'account-1',
    user_id: 'user',
    file_path: filePath,
    caption: 'caption',
    correlation_id: 'image-correlation',
  });
  assert.equal(response.ok, true);
  assert.equal(sent.length, 2);
  assert.equal(sent[0].item_list[0].text_item.text, 'caption');
  const imageItem = sent[1].item_list[0].image_item;
  assert.equal(imageItem.media.encrypt_query_param, 'download-parameter');
  const decodedKey = Buffer.from(imageItem.media.aes_key, 'base64').toString('ascii');
  assert.match(decodedKey, /^[0-9a-f]{32}$/);
  assert.equal(uploadRequest.aeskey, decodedKey);
  assert.equal(imageItem.mid_size, encryptedUpload.length);
  assert.deepEqual(decryptEcb(encryptedUpload, Buffer.from(decodedKey, 'hex')), plaintext);
  assert.deepEqual(sent.map(message => message.client_id), [
    stableClientId('image-correlation', 0),
    stableClientId('image-correlation', 1),
  ]);
  await bridge.stop();
});

test('outbound symlink is rejected before any provider request', async t => {
  let requests = 0;
  const api = await fakeServer(async (_request, response) => {
    requests += 1;
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-symlink-');
  const mediaRoot = path.join(directory, 'outbound');
  fs.mkdirSync(mediaRoot, {mode: 0o700});
  const outside = path.join(directory, 'outside.png');
  fs.writeFileSync(outside, Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1]));
  const linked = path.join(mediaRoot, 'linked.png');
  fs.symlinkSync(outside, linked);
  const state = accountState(api.baseUrl);
  state.context_tokens['account-1'] = {user: {token: 'context', updated_at_ms: 1}};
  writePrivateState(directory, state);
  const bridge = new BridgeHarness({stateDir: directory, mediaRoot});
  const response = await bridge.request('send_image', {
    account_id: 'account-1',
    user_id: 'user',
    file_path: linked,
    correlation_id: 'symlink',
  });
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'media_invalid');
  assert.equal(requests, 0);
  await bridge.stop();
});
