import {spawn} from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const BRIDGE_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
);

export function temporaryDirectory(prefix = 'nanoclaw-weixin-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export async function fakeServer(handler) {
  const server = http.createServer((request, response) => {
    Promise.resolve(handler(request, response)).catch(err => {
      response.statusCode = 500;
      response.end(JSON.stringify({error: String(err)}));
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    server,
    async close() {
      server.closeAllConnections?.();
      await new Promise(resolve => server.close(resolve));
    },
  };
}

export async function jsonBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

export function jsonResponse(response, value, status = 200, headers = {}) {
  response.writeHead(status, {'Content-Type': 'application/json', ...headers});
  response.end(JSON.stringify(value));
}

export class BridgeHarness {
  constructor({stateDir, mediaRoot, env = {}}) {
    this.lines = [];
    this.waiters = new Set();
    this.stderr = '';
    this.sequence = 0;
    this.child = spawn(process.execPath, ['bridge.mjs'], {
      cwd: BRIDGE_DIR,
      env: {
        ...process.env,
        NANOCLAW_WEIXIN_STATE_DIR: stateDir,
        ...(mediaRoot ? {NANOCLAW_WEIXIN_MEDIA_ROOT: mediaRoot} : {}),
        ...env,
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', chunk => {
      stdout += chunk;
      while (stdout.includes('\n')) {
        const index = stdout.indexOf('\n');
        const raw = stdout.slice(0, index);
        stdout = stdout.slice(index + 1);
        if (!raw) continue;
        const line = JSON.parse(raw);
        this.lines.push(line);
        for (const waiter of [...this.waiters]) waiter(line);
      }
    });
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', chunk => {
      this.stderr += chunk;
    });
    this.exit = new Promise((resolve, reject) => {
      this.child.once('exit', (code, signal) => resolve({code, signal}));
      this.child.once('error', reject);
    });
  }

  write(value) {
    this.child.stdin.write(`${typeof value === 'string' ? value : JSON.stringify(value)}\n`);
  }

  async request(method, params = {}, id = `req-${++this.sequence}`) {
    this.write({v: 1, type: 'request', id, method, params});
    return this.waitFor(line => line.type === 'response' && line.id === id);
  }

  async event(name, predicate = () => true) {
    return this.waitFor(
      line => line.type === 'event' && line.event === name && predicate(line.data || {}),
    );
  }

  async waitFor(predicate, timeoutMs = 3000) {
    const existing = this.lines.find(predicate);
    if (existing) return existing;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(check);
        reject(new Error(`bridge output timeout; stderr=${this.stderr}`));
      }, timeoutMs);
      const check = line => {
        if (!predicate(line)) return;
        clearTimeout(timer);
        this.waiters.delete(check);
        resolve(line);
      };
      this.waiters.add(check);
    });
  }

  async terminate(signal = 'SIGKILL') {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return this.exit;
    this.child.kill(signal);
    return this.exit;
  }

  async stop() {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return this.exit;
    await this.request('stop');
    this.child.stdin.end();
    return this.exit;
  }
}

export function accountState(baseUrl, overrides = {}) {
  return {
    version: 1,
    account: {
      account_id: 'account-1',
      bot_token: 'bot-secret',
      base_url: baseUrl,
      ...overrides,
    },
    cursor: 'cursor-old',
    context_tokens: {},
    processed_message_ids: [],
  };
}

export function writePrivateState(directory, state) {
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  fs.writeFileSync(path.join(directory, 'state.json'), JSON.stringify(state), {
    mode: 0o600,
  });
}

export async function eventually(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = predicate();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error('condition was not reached');
}

export function stableClientId(correlationId, part) {
  return crypto
    .createHash('sha256')
    .update(`${correlationId}:${part}`)
    .digest('hex')
    .slice(0, 32);
}
