import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {encryptEcb} from '../lib/cdn.mjs';

import {
  accountState,
  BridgeHarness,
  eventually,
  fakeServer,
  jsonBody,
  jsonResponse,
  temporaryDirectory,
  writePrivateState,
} from './harness.mjs';

function loadState(directory) {
  return JSON.parse(fs.readFileSync(path.join(directory, 'state.json'), 'utf8'));
}

function message(id, user = 'user') {
  return {
    message_id: id,
    from_user_id: user,
    context_token: `context-${user}`,
    item_list: [{type: 1, text_item: {text: `hello-${id}`}}],
  };
}

test('unacked child crash leaves cursor old and restart redelivers', async t => {
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    assert.equal(request.url, '/ilink/bot/getupdates');
    jsonResponse(response, {
      ret: 0,
      get_updates_buf: body.get_updates_buf === 'cursor-old' ? 'cursor-next' : body.get_updates_buf,
      msgs: body.get_updates_buf === 'cursor-old' ? [message('message-1')] : [],
    });
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-crash-');
  writePrivateState(directory, accountState(api.baseUrl));

  const first = new BridgeHarness({stateDir: directory});
  await first.request('start');
  const original = await first.event('inbound_message');
  await first.terminate();
  const afterCrash = loadState(directory);
  assert.equal(afterCrash.cursor, 'cursor-old');
  assert.deepEqual(afterCrash.processed_message_ids, []);
  assert.equal(afterCrash.context_tokens['account-1'].user.token, 'context-user');

  const second = new BridgeHarness({stateDir: directory});
  await second.request('start');
  const redelivered = await second.event('inbound_message');
  assert.equal(redelivered.data.delivery_id, original.data.delivery_id);
  await second.request('ack_inbound', {delivery_id: redelivered.data.delivery_id});
  await eventually(() => loadState(directory).cursor === 'cursor-next');
  await second.stop();
});

test('multi-message batch advances cursor only after every ack', async t => {
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    jsonResponse(response, {
      ret: 0,
      get_updates_buf: 'cursor-next',
      msgs: body.get_updates_buf === 'cursor-old'
        ? [message('message-1'), message('message-2')]
        : [],
    });
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-batch-');
  writePrivateState(directory, accountState(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  const first = await bridge.event('inbound_message', data => data.message_id === 'message-1');
  const second = await bridge.event('inbound_message', data => data.message_id === 'message-2');
  await bridge.request('ack_inbound', {delivery_id: first.data.delivery_id});
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(loadState(directory).cursor, 'cursor-old');
  await bridge.request('ack_inbound', {delivery_id: second.data.delivery_id});
  const committed = await eventually(() => {
    const state = loadState(directory);
    return state.cursor === 'cursor-next' ? state : null;
  });
  assert.equal(committed.processed_message_ids.length, 2);
  assert.ok(committed.processed_message_ids.every(key => key.startsWith('["account-1",')));
  await bridge.stop();
});

test('provider -14 clears credentials, emits expiry, and stops polling', async t => {
  let polls = 0;
  const api = await fakeServer(async (request, response) => {
    await jsonBody(request);
    polls += 1;
    jsonResponse(response, {ret: 0, errcode: -14});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-expired-');
  const original = accountState(api.baseUrl);
  original.context_tokens['account-1'] = {
    user: {token: 'stale-context', updated_at_ms: 1},
  };
  original.processed_message_ids = ['["account-1","old"]'];
  writePrivateState(directory, original);
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  const expired = await bridge.event('session_expired');
  assert.equal(expired.data.code, -14);
  const invalidated = await eventually(() => {
    const current = loadState(directory);
    return current.account === null ? current : null;
  });
  assert.deepEqual(invalidated.context_tokens, {});
  assert.deepEqual(invalidated.processed_message_ids, []);
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(polls, 1);
  await bridge.stop();
});

test('-14 then relogin cannot reuse the previous session context token', async t => {
  let sends = 0;
  const api = await fakeServer(async (request, response) => {
    if (request.url === '/ilink/bot/getupdates') {
      await jsonBody(request);
      jsonResponse(response, {ret: 0, errcode: -14});
      return;
    }
    if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
      await jsonBody(request);
      jsonResponse(response, {
        qrcode: 'qr',
        qrcode_img_content: 'https://example.invalid/qr',
      });
      return;
    }
    if (request.url.startsWith('/ilink/bot/get_qrcode_status')) {
      jsonResponse(response, {
        status: 'confirmed',
        ilink_bot_id: 'account-1',
        bot_token: 'new-bot-token',
        baseurl: api.baseUrl,
      });
      return;
    }
    if (request.url === '/ilink/bot/sendmessage') {
      sends += 1;
      jsonResponse(response, {ret: 0});
      return;
    }
    response.statusCode = 404;
    response.end();
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-expired-context-');
  const original = accountState(api.baseUrl);
  original.context_tokens['account-1'] = {
    user: {token: 'stale-context', updated_at_ms: 1},
  };
  writePrivateState(directory, original);
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  await bridge.event('session_expired');
  const login = await bridge.request('login', {
    force: true,
    base_url: api.baseUrl,
    timeout_ms: 2000,
  });
  assert.equal(login.ok, true);
  const send = await bridge.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'must not send',
    correlation_id: 'stale-context',
  });
  assert.equal(send.ok, false);
  assert.equal(send.error.code, 'context_missing');
  assert.equal(sends, 0);
  await bridge.stop();
});

test('inbound interaction persists context for proactive send after process restart', async t => {
  const sent = [];
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    if (request.url === '/ilink/bot/getupdates') {
      jsonResponse(response, {
        ret: 0,
        get_updates_buf: 'cursor-next',
        msgs: body.get_updates_buf === 'cursor-old' ? [message('message-1')] : [],
      });
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
  const directory = temporaryDirectory('wx-proactive-');
  writePrivateState(directory, accountState(api.baseUrl));
  const first = new BridgeHarness({stateDir: directory});
  await first.request('start');
  const inbound = await first.event('inbound_message');
  await first.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await eventually(() => loadState(directory).cursor === 'cursor-next');
  await first.stop();

  const second = new BridgeHarness({stateDir: directory});
  const response = await second.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'proactive',
    correlation_id: 'after-restart',
  });
  const receipt = await second.event(
    'delivery_result',
    data => data.correlation_id === 'after-restart',
  );
  assert.equal(response.ok, true);
  assert.equal(receipt.data.success, true);
  assert.equal(sent[0].context_token, 'context-user');
  await second.stop();
});

test('inbound encrypted image is downloaded to a private controlled file', async t => {
  const plaintext = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1]);
  const key = Buffer.from('0123456789abcdef');
  const encrypted = encryptEcb(plaintext, key);
  const cdn = await fakeServer(async (request, response) => {
    assert.match(request.url, /^\/c2c\/download\?/);
    response.writeHead(200, {'Content-Type': 'application/octet-stream'});
    response.end(encrypted);
  });
  t.after(() => cdn.close());
  const api = await fakeServer(async (request, response) => {
    await jsonBody(request);
    jsonResponse(response, {
      ret: 0,
      get_updates_buf: 'cursor-next',
      msgs: [{
        ...message('image-message'),
        item_list: [{
          type: 2,
          image_item: {
            media: {
              encrypt_query_param: 'download',
              aes_key: key.toString('base64'),
            },
          },
        }],
      }],
    });
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-inbound-image-');
  writePrivateState(
    directory,
    accountState(api.baseUrl, {cdn_base_url: `${cdn.baseUrl}/c2c`}),
  );
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  const inbound = await bridge.event('inbound_message');
  assert.equal(inbound.data.images.length, 1);
  assert.equal(inbound.data.images[0].mime_type, 'image/png');
  const filePath = inbound.data.images[0].file_path;
  assert.deepEqual(fs.readFileSync(filePath), plaintext);
  assert.equal(fs.statSync(filePath).mode & 0o777, 0o600);
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});
