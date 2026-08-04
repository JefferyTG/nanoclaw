import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

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

function typingParams(activityId) {
  return {
    account_id: 'account-1',
    user_id: 'user',
    activity_id: activityId,
  };
}

function stateWithContext(baseUrl) {
  const state = accountState(baseUrl);
  state.context_tokens['account-1'] = {
    user: {token: 'context-secret', updated_at_ms: 1},
  };
  return state;
}

test('typing activity keeps ticket/context inside Bridge and cancels only after last activity', async t => {
  const calls = [];
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    calls.push({url: request.url, body});
    if (request.url === '/ilink/bot/getconfig') {
      jsonResponse(response, {ret: 0, typing_ticket: 'ticket-secret'});
      return;
    }
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());

  const directory = temporaryDirectory('wx-typing-overlap-');
  writePrivateState(directory, stateWithContext(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});

  const first = await bridge.request('typing_start', typingParams('activity-a'));
  assert.equal(first.ok, true);
  await eventually(() => calls.some(
    call => call.url === '/ilink/bot/sendtyping' && call.body.status === 1,
  ));
  const second = await bridge.request('typing_start', typingParams('activity-b'));
  assert.equal(second.ok, true);

  const firstStop = await bridge.request('typing_stop', typingParams('activity-a'));
  assert.equal(firstStop.ok, true);
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(
    calls.filter(call => call.url === '/ilink/bot/sendtyping' && call.body.status === 2).length,
    0,
  );

  const lastStop = await bridge.request('typing_stop', typingParams('activity-b'));
  assert.equal(lastStop.ok, true);
  await eventually(() => calls.some(
    call => call.url === '/ilink/bot/sendtyping' && call.body.status === 2,
  ));

  const output = JSON.stringify(bridge.lines);
  assert.equal(output.includes('ticket-secret'), false);
  assert.equal(output.includes('context-secret'), false);
  assert.equal(calls.find(call => call.url === '/ilink/bot/getconfig').body.context_token, 'context-secret');
  assert.equal(
    calls.find(call => call.url === '/ilink/bot/sendtyping').body.typing_ticket,
    'ticket-secret',
  );
  await bridge.stop();
});

test('long typing activity renews and stop cleanup is bounded', async t => {
  const calls = [];
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    calls.push({url: request.url, body});
    if (request.url === '/ilink/bot/getconfig') {
      jsonResponse(response, {ret: 0, typing_ticket: 'renew-ticket'});
      return;
    }
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());

  const directory = temporaryDirectory('wx-typing-renew-');
  writePrivateState(directory, stateWithContext(api.baseUrl));
  const bridge = new BridgeHarness({
    stateDir: directory,
    env: {NANOCLAW_WEIXIN_TYPING_RENEW_MS: '20'},
  });
  await bridge.request('typing_start', typingParams('long-task'));
  await eventually(
    () => calls.filter(call => call.url === '/ilink/bot/sendtyping' && call.body.status === 1).length >= 2,
    1000,
  );

  const started = Date.now();
  await bridge.request('typing_stop', typingParams('long-task'));
  assert.ok(Date.now() - started < 1000);
  await eventually(() => calls.some(
    call => call.url === '/ilink/bot/sendtyping' && call.body.status === 2,
  ));
  await bridge.stop();
});

test('typing provider failure retries without blocking final send and cleanup', async t => {
  const calls = [];
  let typingAttempts = 0;
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    calls.push({url: request.url, body});
    if (request.url === '/ilink/bot/getconfig') {
      jsonResponse(response, {ret: 0, typing_ticket: 'failure-ticket'});
      return;
    }
    if (request.url === '/ilink/bot/sendtyping' && body.status === 1) {
      typingAttempts += 1;
      if (typingAttempts === 1) {
        jsonResponse(response, {ret: 0, errcode: 503});
        return;
      }
    }
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());

  const directory = temporaryDirectory('wx-typing-failure-');
  writePrivateState(directory, stateWithContext(api.baseUrl));
  const bridge = new BridgeHarness({
    stateDir: directory,
    env: {
      NANOCLAW_WEIXIN_TYPING_RETRY_MS: '10',
      NANOCLAW_WEIXIN_TYPING_CLEANUP_TIMEOUT_MS: '50',
    },
  });
  const start = await bridge.request('typing_start', typingParams('retry-task'));
  assert.equal(start.ok, false);
  assert.equal(start.error.code, 'api_rejected');

  const send = await bridge.request('send_text', {
    account_id: 'account-1',
    user_id: 'user',
    text: 'final answer',
    correlation_id: 'final-answer',
  });
  assert.equal(send.ok, true);
  await eventually(() => typingAttempts >= 2);
  const stop = await bridge.request('typing_stop', typingParams('retry-task'));
  assert.equal(stop.ok, true);
  await eventually(() => calls.some(
    call => call.url === '/ilink/bot/sendtyping' && call.body.status === 2,
  ));

  const sendIndex = calls.findIndex(call => call.url === '/ilink/bot/sendmessage');
  const cancelIndex = calls.findIndex(
    call => call.url === '/ilink/bot/sendtyping' && call.body.status === 2,
  );
  assert.ok(sendIndex >= 0);
  assert.ok(cancelIndex > sendIndex);
  await bridge.stop();
});

test('session expiry clears typing activity and durable account state', async t => {
  const api = await fakeServer(async (request, response) => {
    await jsonBody(request);
    if (request.url === '/ilink/bot/getconfig') {
      jsonResponse(response, {ret: 0, errcode: -14});
      return;
    }
    jsonResponse(response, {ret: 0});
  });
  t.after(() => api.close());

  const directory = temporaryDirectory('wx-typing-expired-');
  writePrivateState(directory, stateWithContext(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});
  const response = await bridge.request('typing_start', typingParams('expired-task'));
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'session_expired');
  await bridge.event('session_expired');
  const saved = JSON.parse(fs.readFileSync(path.join(directory, 'state.json'), 'utf8'));
  assert.equal(saved.account, null);
  assert.deepEqual(saved.context_tokens, {});
  await bridge.stop();
});
