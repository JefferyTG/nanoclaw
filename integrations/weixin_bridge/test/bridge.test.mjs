import assert from 'node:assert/strict';
import test from 'node:test';

import {BridgeHarness, temporaryDirectory} from './harness.mjs';

test('JSONL child emits protocol-only output and handles hello/stop', async () => {
  const bridge = new BridgeHarness({stateDir: temporaryDirectory('wx-ipc-')});
  const ready = await bridge.event('ready');
  assert.equal(ready.v, 1);
  const hello = await bridge.request('hello');
  assert.equal(hello.ok, true);
  assert.equal(hello.result.bridge_version, '1.0.0');
  assert.equal(hello.result.protocol_version, 1);
  await bridge.stop();
  assert.equal(bridge.stderr, '');
  assert.ok(bridge.lines.every(line => line.v === 1));
});

test('invalid and oversized JSONL input is rejected structurally', async () => {
  const bridge = new BridgeHarness({
    stateDir: temporaryDirectory('wx-ipc-limit-'),
    env: {NANOCLAW_WEIXIN_MAX_LINE_BYTES: '128'},
  });
  bridge.write('{not-json');
  const invalid = await bridge.waitFor(
    line => line.type === 'response' && line.id === null,
  );
  assert.equal(invalid.error.code, 'protocol_error');
  bridge.write('x'.repeat(256));
  const failures = await bridge.waitFor(
    line => line.type === 'response'
      && line.id === null
      && bridge.lines.filter(candidate => candidate.type === 'response').length >= 2,
  );
  assert.equal(failures.error.code, 'protocol_error');
  await bridge.stop();
});
