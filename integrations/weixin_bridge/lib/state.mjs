import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const EMPTY_STATE = () => ({
  version: 1,
  account: null,
  cursor: '',
  context_tokens: {},
  processed_message_ids: [],
});

export class StateStore {
  constructor(directory, {maxProcessed = 2048} = {}) {
    this.dir = directory;
    this.file = path.join(directory, 'state.json');
    this.maxProcessed = maxProcessed;
  }

  ensureDirectory() {
    fs.mkdirSync(this.dir, {recursive: true, mode: 0o700});
    const info = fs.lstatSync(this.dir);
    if (!info.isDirectory() || info.isSymbolicLink()) {
      throw new Error('invalid state directory');
    }
    fs.chmodSync(this.dir, 0o700);
  }

  load() {
    this.ensureDirectory();
    if (!fs.existsSync(this.file)) return EMPTY_STATE();
    const descriptor = fs.openSync(
      this.file,
      fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0),
    );
    let saved;
    try {
      const info = fs.fstatSync(descriptor);
      if (!info.isFile()) throw new Error('invalid state file');
      saved = JSON.parse(fs.readFileSync(descriptor, 'utf8'));
    } finally {
      fs.closeSync(descriptor);
    }
    if (saved.version !== 1) throw new Error('unsupported_state');
    const state = {...EMPTY_STATE(), ...saved};
    if (
      !state.context_tokens
      || typeof state.context_tokens !== 'object'
      || !Array.isArray(state.processed_message_ids)
      || typeof state.cursor !== 'string'
    ) {
      throw new Error('invalid state schema');
    }
    return state;
  }

  save(state) {
    this.ensureDirectory();
    const temporary = `${this.file}.${process.pid}.${Date.now()}.${cryptoSuffix()}.tmp`;
    let descriptor;
    try {
      descriptor = fs.openSync(temporary, 'wx', 0o600);
      fs.writeFileSync(descriptor, JSON.stringify(state));
      fs.fsyncSync(descriptor);
      fs.closeSync(descriptor);
      descriptor = undefined;
      fs.renameSync(temporary, this.file);
      fs.chmodSync(this.file, 0o600);
      try {
        const directory = fs.openSync(this.dir, 'r');
        try {
          fs.fsyncSync(directory);
        } finally {
          fs.closeSync(directory);
        }
      } catch {
        // Some filesystems do not support directory fsync; the file itself is durable.
      }
    } finally {
      if (descriptor !== undefined) fs.closeSync(descriptor);
      try {
        fs.unlinkSync(temporary);
      } catch (err) {
        if (err?.code !== 'ENOENT') throw err;
      }
    }
  }

  rememberContext(state, accountId, userId, token, now = Date.now()) {
    state.context_tokens[accountId] ??= {};
    state.context_tokens[accountId][userId] = {
      token,
      updated_at_ms: now,
    };
    this.save(state);
  }

  ack(state, id, cursor) {
    if (!state.processed_message_ids.includes(id)) {
      state.processed_message_ids.push(id);
    }
    state.processed_message_ids = state.processed_message_ids.slice(-this.maxProcessed);
    if (cursor !== undefined) state.cursor = cursor;
    this.save(state);
  }

  invalidate(state) {
    state.account = null;
    state.cursor = '';
    // V1 owns one account. A session generation change invalidates every
    // context token and dedupe key learned under that credential generation.
    state.context_tokens = {};
    state.processed_message_ids = [];
    this.save(state);
  }
}

function cryptoSuffix() {
  return crypto.randomBytes(8).toString('hex');
}
