import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
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

test('login refreshes QR, follows redirect host, and adopts confirmed base URL', async t => {
  let qrRequests = 0;
  let initialStatusRequests = 0;
  const redirected = await fakeServer(async (request, response) => {
    assert.match(request.url, /get_qrcode_status/);
    jsonResponse(response, {
      status: 'confirmed',
      ilink_bot_id: 'bot-account',
      ilink_user_id: 'login-user',
      bot_token: 'bot-token',
      route_tag: 'route-1',
    });
  });
  t.after(() => redirected.close());
  const initial = await fakeServer(async (request, response) => {
    if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
      assert.equal(request.method, 'POST');
      assert.equal(request.headers.authorizationtype, 'ilink_bot_token');
      assert.match(
        Buffer.from(request.headers['x-wechat-uin'], 'base64').toString(),
        /^\d+$/,
      );
      assert.equal(request.headers['ilink-app-id'], 'bot');
      assert.equal(request.headers['ilink-app-clientversion'], '65536');
      assert.deepEqual((await jsonBody(request)).local_token_list, []);
      qrRequests += 1;
      jsonResponse(response, {
        qrcode: `qr-${qrRequests}`,
        qrcode_img_content: `https://example.invalid/qr-${qrRequests}`,
      });
      return;
    }
    if (request.url.includes('qrcode=qr-1')) {
      initialStatusRequests += 1;
      jsonResponse(response, initialStatusRequests === 1
        ? {status: 'wait'}
        : initialStatusRequests === 2
          ? {status: 'scaned'}
          : {status: 'expired'});
      return;
    }
    if (request.url.includes('qrcode=qr-2')) {
      jsonResponse(response, {
        status: 'scaned_but_redirect',
        redirect_host: redirected.baseUrl,
      });
      return;
    }
    response.statusCode = 404;
    response.end();
  });
  t.after(() => initial.close());
  const directory = temporaryDirectory('wx-login-');
  const bridge = new BridgeHarness({stateDir: directory});
  const response = await bridge.request('login', {
    force: true,
    base_url: initial.baseUrl,
    timeout_ms: 5000,
  });
  assert.equal(response.ok, true);
  assert.equal(response.result.account_id, 'bot-account');
  const qrEvents = bridge.lines.filter(
    line => line.type === 'event' && line.event === 'qr_code',
  );
  assert.equal(qrEvents.length, 2);
  assert.ok(qrEvents.every(line => line.data.ascii.length > 0));
  const saved = JSON.parse(fs.readFileSync(path.join(directory, 'state.json'), 'utf8'));
  assert.equal(saved.account.account_id, 'bot-account');
  assert.equal(saved.account.base_url, redirected.baseUrl);
  assert.equal(saved.account.route_tag, 'route-1');
  assert.equal(saved.account.login_user_id, 'login-user');
  await bridge.stop();
});

test('login reports need_verifycode as an explicit unsupported verification state', async t => {
    const api = await fakeServer(async (request, response) => {
      if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
        await jsonBody(request);
        jsonResponse(response, {qrcode: 'qr', qrcode_img_content: 'https://example.invalid/qr'});
      } else {
        jsonResponse(response, {status: 'need_verifycode'});
      }
    });
    t.after(() => api.close());
    const bridge = new BridgeHarness({stateDir: temporaryDirectory('wx-verify-')});
    const response = await bridge.request('login', {
      force: true,
      base_url: api.baseUrl,
      timeout_ms: 2000,
    });
    assert.equal(response.ok, false);
    assert.equal(response.error.code, 'verification_required');
    await bridge.stop();
});

test('verify_code_blocked refreshes QR with a bounded retry count', async t => {
  let qrRequests = 0;
  const api = await fakeServer(async (request, response) => {
    if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
      await jsonBody(request);
      qrRequests += 1;
      jsonResponse(response, {
        qrcode: `qr-${qrRequests}`,
        qrcode_img_content: `https://example.invalid/qr-${qrRequests}`,
      });
    } else {
      jsonResponse(response, {status: 'verify_code_blocked'});
    }
  });
  t.after(() => api.close());
  const bridge = new BridgeHarness({stateDir: temporaryDirectory('wx-blocked-')});
  const response = await bridge.request('login', {
    force: true,
    base_url: api.baseUrl,
    timeout_ms: 5000,
  });
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'verification_required');
  assert.equal(qrRequests, 4);
  assert.equal(
    bridge.lines.filter(line => line.type === 'event' && line.event === 'qr_code').length,
    4,
  );
  await bridge.stop();
});

test('login reports binded_redirect as unrecoverable without local credentials', async t => {
  const api = await fakeServer(async (request, response) => {
    if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
      await jsonBody(request);
      jsonResponse(response, {qrcode: 'qr', qrcode_img_content: 'https://example.invalid/qr'});
    } else {
      jsonResponse(response, {status: 'binded_redirect'});
    }
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-bound-');
  const bridge = new BridgeHarness({stateDir: directory});
  const response = await bridge.request('login', {
    force: true,
    base_url: api.baseUrl,
    timeout_ms: 2000,
  });
  assert.equal(response.ok, false);
  assert.equal(response.error.code, 'already_bound_unrecoverable');
  assert.equal(fs.existsSync(path.join(directory, 'state.json')), false);
  await bridge.stop();
});

test('binded_redirect preserves and resumes recoverable local credentials', async t => {
  const api = await fakeServer(async (request, response) => {
    if (request.url.startsWith('/ilink/bot/get_bot_qrcode')) {
      const body = await jsonBody(request);
      assert.deepEqual(body.local_token_list, ['bot-secret']);
      jsonResponse(response, {qrcode: 'qr', qrcode_img_content: 'https://example.invalid/qr'});
    } else {
      jsonResponse(response, {status: 'binded_redirect'});
    }
  });
  t.after(() => api.close());
  const directory = temporaryDirectory('wx-bound-resume-');
  const original = accountState(api.baseUrl);
  writePrivateState(directory, original);
  const bridge = new BridgeHarness({stateDir: directory});
  const response = await bridge.request('login', {
    force: true,
    base_url: api.baseUrl,
    timeout_ms: 2000,
  });
  assert.equal(response.ok, true);
  assert.equal(response.result.resumed, true);
  assert.equal(response.result.already_connected, true);
  const saved = JSON.parse(fs.readFileSync(path.join(directory, 'state.json'), 'utf8'));
  assert.equal(saved.account.bot_token, 'bot-secret');
  await bridge.stop();
});
