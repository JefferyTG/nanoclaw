import assert from 'node:assert/strict';
import test from 'node:test';

import {
  accountState,
  BridgeHarness,
  fakeServer,
  jsonBody,
  jsonResponse,
  temporaryDirectory,
  writePrivateState,
} from './harness.mjs';

// MessageItemType.VOICE == 3; voice_item.text carries the Tencent STT
// transcript for the voice message (silver line on 2026-08-04:
// "哈喽哈喽，小奈在吗" matched the audio word for word).
function voiceItem(text) {
  const item = {
    type: 3,
    voice_item: {
      encode_type: 6, // silk
      sample_rate: 16000,
      playtime: 2706,
    },
  };
  if (text !== undefined) item.voice_item.text = text;
  return item;
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

async function runInbound(itemList, messageId = 'voice-message') {
  const api = await fakeServer(async (request, response) => {
    const body = await jsonBody(request);
    jsonResponse(response, {
      ret: 0,
      get_updates_buf: 'cursor-next',
      msgs: body.get_updates_buf === 'cursor-old'
        ? [inboundMessage(messageId, itemList)]
        : [],
    });
  });
  const directory = temporaryDirectory('wx-voice-');
  writePrivateState(directory, accountState(api.baseUrl));
  const bridge = new BridgeHarness({stateDir: directory});
  await bridge.request('start');
  const inbound = await bridge.event('inbound_message');
  return {api, bridge, inbound};
}

test('TEXT item + VOICE transcript merge into inbound text, newline separated', async t => {
  const {api, bridge, inbound} = await runInbound([
    textItem('看下这个'),
    voiceItem('哈喽哈喽，小奈在吗'),
  ]);
  t.after(() => api.close());
  assert.equal(inbound.data.text, '看下这个\n哈喽哈喽，小奈在吗');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('pure voice message exposes the STT transcript as inbound text', async t => {
  const {api, bridge, inbound} = await runInbound([
    voiceItem('哈喽哈喽，小奈在吗'),
  ]);
  t.after(() => api.close());
  assert.equal(inbound.data.text, '哈喽哈喽，小奈在吗');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('VOICE item without text is ignored; TEXT content unchanged (regression)', async t => {
  const {api, bridge, inbound} = await runInbound([
    textItem('只说文字'),
    voiceItem(undefined),
  ]);
  t.after(() => api.close());
  assert.equal(inbound.data.text, '只说文字');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});

test('mixed TEXT/VOICE items merge stably: TEXT first, transcripts appended after', async t => {
  const {api, bridge, inbound} = await runInbound([
    textItem('文字一'),
    voiceItem('语音一转写'),
    textItem('文字二'),
    voiceItem('   '), // whitespace-only transcript is ignored
    voiceItem('语音二转写'),
  ]);
  t.after(() => api.close());
  assert.equal(inbound.data.text, '文字一\n文字二\n语音一转写\n语音二转写');
  await bridge.request('ack_inbound', {delivery_id: inbound.data.delivery_id});
  await bridge.stop();
});
