import assert from 'node:assert/strict';
import test from 'node:test';
import {accountState, BridgeHarness, eventually, fakeServer, jsonBody, jsonResponse, temporaryDirectory, writePrivateState} from './harness.mjs';

test('getUpdates accepts a successful body without ret or errcode', async t => {
  let polls = 0;
  const api = await fakeServer(async (request, response) => {
    await jsonBody(request);
    assert.equal(request.url, '/ilink/bot/getupdates');
    polls += 1;
    jsonResponse(response, {get_updates_buf: 'cursor-next', msgs: []});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-poll-accept-');
  writePrivateState(directory, accountState(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  await eventually(() => polls >= 2);
  assert.equal(bridge.lines.some(line => line.event === 'channel_error'), false);
  await bridge.stop();
});

test('protocol diagnostics are stable and do not expose provider response text', async t => {
  const api = await fakeServer(async (request, response) => {
    await jsonBody(request);
    jsonResponse(response, {msgs: {secret_cursor: 'do-not-leak'}});
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-poll-diagnostic-');
  writePrivateState(directory, accountState(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  const event = await bridge.event('channel_error');
  assert.equal(event.data.code, 'protocol_error');
  assert.equal(event.data.reason, 'invalid_messages');
  assert.equal(JSON.stringify(event.data).includes('do-not-leak'), false);
  await bridge.stop();
});
