/**
 * Project-specific no-digest storage for the official DSH SessionPersistence seam.
 * Replay, event validation and interrupted-turn repair remain upstream-owned.
 */
import {
  closeSync, existsSync, fstatSync, fsyncSync, ftruncateSync, mkdirSync,
  openSync, readFileSync, realpathSync, statSync, unlinkSync, writeSync,
} from 'node:fs';
import { hostname } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { TextDecoder } from 'node:util';
import SessionPersistence, {
  SessionAlreadyExistsError, SessionAlreadyOwnedError, SessionHandleClosedError,
  SessionOwnershipLostError, SessionPersistenceCorruptionError,
  SessionPersistenceNotFoundError, SessionPersistenceRevision, SessionReadOnlyError,
  assertContiguous, assertStoredId, assertVersion, materializeAppendBatch,
  materializeCreateHeader, validateStoredEvents,
} from '@deepseek-ai/dsh-session-persistence';

const FORMAT = 'third-party-review-dsh-session';
const FORMAT_VERSION = 1;
const HEADER = 'session-header.json';
const LOG = 'session.jsonl';
const LOCK = 'writer.lock';
const utf8 = new TextDecoder('utf-8', { fatal: true });

function canonical(path) {
  const absolute = resolve(path);
  if (existsSync(absolute)) return realpathSync(absolute);
  const parent = dirname(absolute);
  return parent === absolute ? absolute : join(canonical(parent), relative(parent, absolute));
}
function inside(path, parent) {
  const part = relative(parent, path);
  return part === '' || (!isAbsolute(part) && part !== '..' && !part.startsWith(`..${process.platform === 'win32' ? '\\' : '/'}`));
}
function checkSignal(options) { options?.signal?.throwIfAborted(); }
function corruption(message, cause) {
  return new SessionPersistenceCorruptionError(message, { cause: cause ?? new Error('Invalid persisted session record') });
}
function writeAll(fd, data, position) {
  const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data, 'utf8');
  let offset = 0;
  while (offset < bytes.length) {
    const written = writeSync(fd, bytes, offset, bytes.length - offset, position === undefined ? null : position + offset);
    if (written < 1) throw new Error('Session log write made no progress');
    offset += written;
  }
}
function writeNew(path, data) {
  const fd = openSync(path, 'wx', 0o600);
  try { writeAll(fd, data); fsyncSync(fd); }
  finally { closeSync(fd); }
}
function metadata(root) {
  const headerPath = join(root, HEADER);
  const logPath = join(root, LOG);
  if (!existsSync(headerPath) && !existsSync(logPath)) return undefined;
  if (!existsSync(headerPath) || !existsSync(logPath)) throw corruption('Session metadata/log pair is incomplete');
  let stored;
  try { stored = JSON.parse(readFileSync(headerPath, 'utf8')); }
  catch { throw corruption('Session metadata is not valid JSON'); }
  if (stored?.format !== FORMAT || stored?.formatVersion !== FORMAT_VERSION) {
    throw corruption('Unsupported review session storage format');
  }
  let header;
  try { header = materializeCreateHeader(stored.header); }
  catch { throw corruption('Session metadata has an invalid header'); }
  if (typeof header.id !== 'string' || !header.id || typeof header.isSeeded !== 'boolean') {
    throw corruption('Session metadata has an invalid identity or lineage');
  }
  assertVersion(header, { kind: FORMAT, path: logPath });
  const inheritedEventCount = stored.inheritedEventCount;
  if (!Number.isSafeInteger(inheritedEventCount) || inheritedEventCount < 0
      || (!header.isSeeded && inheritedEventCount !== 0)) {
    throw corruption('Invalid inherited session event count');
  }
  return { header, inheritedEventCount, logPath };
}
function readRecord(root) {
  const meta = metadata(root);
  if (meta === undefined) return undefined;
  const bytes = readFileSync(meta.logPath);
  const lastNewline = bytes.lastIndexOf(0x0a);
  const validBytes = lastNewline + 1;
  const complete = bytes.subarray(0, validBytes);
  let text;
  try { text = utf8.decode(complete); }
  catch { throw corruption('Session log contains invalid UTF-8 before its physical tail'); }
  const events = [];
  for (const line of text.split('\n').slice(0, -1)) {
    let event;
    try { event = JSON.parse(line); }
    catch { throw corruption(`Session log contains invalid JSON at logical line ${events.length + 1}`); }
    events.push(event);
  }
  assertContiguous(meta.header.id, events, 0);
  validateStoredEvents(meta.header, events, { kind: FORMAT, path: meta.logPath });
  if (meta.inheritedEventCount > events.length) throw corruption('Session log is shorter than its inherited event prefix');
  return { ...meta, events, validBytes, tail: bytes.subarray(validBytes) };
}
function readLock(root) {
  const path = join(root, LOCK);
  if (!existsSync(path)) return undefined;
  let owner;
  try { owner = JSON.parse(readFileSync(path, 'utf8')); }
  catch { throw corruption('Session writer ownership record is unreadable'); }
  if (!Number.isSafeInteger(owner?.pid) || owner.pid < 1 || typeof owner.host !== 'string'
      || typeof owner.token !== 'string' || !owner.token) throw corruption('Session writer ownership record is invalid');
  return owner;
}
function assertSeedInactive(root, id) {
  const owner = readLock(root);
  if (owner === undefined) return;
  if (owner.host !== hostname()) throw new SessionAlreadyOwnedError(id);
  try { process.kill(owner.pid, 0); }
  catch (error) {
    if (error?.code === 'ESRCH') return;
    throw new SessionAlreadyOwnedError(id);
  }
  throw new SessionAlreadyOwnedError(id);
}
function validateSlice(offset, length) {
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(length) || length < 0) {
    throw new RangeError('Session read offset and length must be non-negative safe integers');
  }
}

/** One review output root holds one official DSH session, without content digests. */
export class DurableSessionPersistence extends SessionPersistence {
  constructor(ctx, config = {}) {
    if (typeof config.root !== 'string' || !config.root) throw new TypeError('A session storage root is required');
    const root = canonical(config.root);
    const seedDirectory = config.seedDirectory === undefined ? undefined : canonical(config.seedDirectory);
    if (seedDirectory !== undefined && (inside(root, seedDirectory) || inside(seedDirectory, root))) {
      throw new RangeError('A resumed session needs a separate output root');
    }
    super(ctx);
    this.root = root;
    this.expectedId = config.sessionId;
    this.activeWriter = undefined;
    this.failure = undefined;
    this.seeded = false;
    this.eventsFile = join(root, LOG);
    if (seedDirectory !== undefined) {
      const seed = readRecord(seedDirectory);
      if (seed === undefined) throw new SessionPersistenceNotFoundError(config.sessionId ?? 'selected-session');
      if (config.sessionId !== undefined) assertStoredId(config.sessionId, seed.header);
      assertSeedInactive(seedDirectory, seed.header.id);
      if (metadata(root) !== undefined || existsSync(join(root, LOCK))) throw new SessionAlreadyExistsError(seed.header.id);
      mkdirSync(root, { recursive: true, mode: 0o700 });
      writeNew(join(root, HEADER), JSON.stringify({ format: FORMAT, formatVersion: FORMAT_VERSION,
        header: seed.header, inheritedEventCount: seed.inheritedEventCount }) + '\n');
      writeNew(this.eventsFile, readFileSync(seed.logPath).subarray(0, seed.validBytes));
      writeNew(join(root, 'seed-info.json'), JSON.stringify({ sourceDirectory: seedDirectory,
        eventCount: seed.events.length, omittedPhysicalTailBytes: seed.tail.length }) + '\n');
      this.seeded = true;
    } else {
      mkdirSync(root, { recursive: true, mode: 0o700 });
    }
    ctx.on('session/event', (session, event) => {
      const row = this.activeWriter;
      if (row === undefined || row.header.id !== session.id || row.closed) return;
      try { this.appendSync(row, [event]); }
      catch (error) { this.failure = error; throw error; }
    });
    ctx.effect(() => async () => { await this.activeWriter?.handle.close(); }, 'review-session-persistence: close writer');
  }
  checkFailure() { if (this.failure !== undefined) throw this.failure; }
  checkId(id) {
    if (this.expectedId !== undefined && this.expectedId !== id) throw new SessionPersistenceNotFoundError(id);
  }
  owner(row) {
    this.checkFailure();
    let current, currentFile, openedFile;
    try {
      current = readLock(this.root);
      currentFile = statSync(this.eventsFile);
      openedFile = fstatSync(row.fd);
    } catch (error) { this.failure = error; throw error; }
    if (current?.token !== row.owner.token || current.pid !== row.owner.pid || current.host !== row.owner.host
        || currentFile.dev !== openedFile.dev || currentFile.ino !== openedFile.ino) {
      throw new SessionOwnershipLostError(row.header.id);
    }
  }
  acquire(id) {
    if (this.activeWriter !== undefined && !this.activeWriter.closed) throw new SessionAlreadyOwnedError(id);
    const owner = { pid: process.pid, host: hostname(), token: randomUUID(), sessionId: id };
    try { writeNew(join(this.root, LOCK), JSON.stringify(owner) + '\n'); }
    catch (error) { if (error?.code === 'EEXIST') throw new SessionAlreadyOwnedError(id); throw error; }
    return owner;
  }
  release(owner) {
    const current = readLock(this.root);
    if (current?.token === owner.token && current.pid === owner.pid && current.host === owner.host) unlinkSync(join(this.root, LOCK));
  }
  appendSync(row, events) {
    this.owner(row);
    const batch = materializeAppendBatch(events);
    assertContiguous(row.header.id, batch, row.events.length);
    validateStoredEvents(row.header, batch, { kind: FORMAT, path: this.eventsFile });
    if (batch.length === 0) return;
    try { writeAll(row.fd, batch.map(event => JSON.stringify(event)).join('\n') + '\n', fstatSync(row.fd).size); }
    catch (error) { this.failure = error; throw error; }
    row.events.push(...batch);
  }
  writer(record, owner) {
    const fd = openSync(this.eventsFile, 'r+', 0o600);
    const row = { ...record, fd, owner, closed: false, handle: undefined };
    try {
      if (row.tail.length > 0) {
        writeNew(join(this.root, `session-tail-${randomUUID()}.bin`), row.tail);
        ftruncateSync(fd, row.validBytes);
        fsyncSync(fd);
      }
    } catch (error) { closeSync(fd); this.release(owner); throw error; }
    const handle = this.handle(record, 'write', row);
    row.handle = handle;
    this.activeWriter = row;
    return handle;
  }
  handle(record, access, row) {
    let closed = false;
    let closePromise;
    let observedCount = 0;
    const check = operation => {
      if (closed) throw new SessionHandleClosedError(record.header.id, operation);
      this.checkFailure();
    };
    const mutate = operation => {
      check(operation);
      if (access !== 'write') throw new SessionReadOnlyError(record.header.id, operation);
      this.owner(row);
    };
    const handle = {
      id: record.header.id, header: structuredClone(record.header),
      inheritedEventCount: record.inheritedEventCount, access,
      read: async (offset = 0, length = Number.MAX_SAFE_INTEGER, options) => {
        check('read'); checkSignal(options); validateSlice(offset, length);
        const current = readRecord(this.root);
        if (current === undefined) throw new SessionPersistenceNotFoundError(record.header.id);
        assertStoredId(record.header.id, current.header);
        if (current.events.length < observedCount) throw corruption('Session read would move behind its observed event prefix');
        observedCount = current.events.length;
        return { eventState: 'detached', events: structuredClone(current.events.slice(offset, offset + length)) };
      },
      append: async (events, options) => {
        mutate('append'); checkSignal(options);
        this.appendSync(row, events);
      },
      flush: async options => {
        mutate('flush'); checkSignal(options);
        try { fsyncSync(row.fd); }
        catch (error) { this.failure = error; throw error; }
      },
      close: () => {
        if (closePromise !== undefined) return closePromise;
        closePromise = Promise.resolve().then(() => {
          if (access === 'read') { closed = true; return; }
          let error = this.failure;
          try { if (error === undefined) { this.owner(row); fsyncSync(row.fd); } }
          catch (cause) { error ??= cause; }
          finally {
            row.closed = true; closed = true;
            try { closeSync(row.fd); } catch (cause) { error ??= cause; }
            try { this.release(row.owner); } catch (cause) { error ??= cause; }
            if (this.activeWriter === row) this.activeWriter = undefined;
          }
          if (error !== undefined) throw error;
        });
        return closePromise;
      },
    };
    handle[Symbol.asyncDispose] = handle.close;
    return handle;
  }
  async create(header, options = {}) {
    checkSignal(options); this.checkFailure(); this.checkId(header.id);
    const stored = materializeCreateHeader(header); assertVersion(stored, { kind: FORMAT, path: this.eventsFile });
    const inheritedEventCount = options.inheritedEventCount ?? 0;
    if (!Number.isSafeInteger(inheritedEventCount) || inheritedEventCount < 0
        || (!stored.isSeeded && inheritedEventCount !== 0)
        || (stored.isSeeded && options.inheritedEventCount === undefined)) throw new TypeError('Invalid inherited event count for session header');
    if (metadata(this.root) !== undefined) throw new SessionAlreadyExistsError(header.id);
    const owner = this.acquire(header.id);
    try {
      if (metadata(this.root) !== undefined) throw new SessionAlreadyExistsError(header.id);
      writeNew(join(this.root, HEADER), JSON.stringify({ format: FORMAT, formatVersion: FORMAT_VERSION, header: stored, inheritedEventCount }) + '\n');
      writeNew(this.eventsFile, '');
      return this.writer({ header: stored, inheritedEventCount, events: [], validBytes: 0, tail: Buffer.alloc(0) }, owner);
    } catch (error) { this.release(owner); throw error; }
  }
  async open(id, access, options = {}) {
    checkSignal(options); this.checkFailure(); this.checkId(id);
    if (access !== 'read' && access !== 'write') throw new TypeError('Session access must be read or write');
    const record = readRecord(this.root);
    if (record === undefined || record.header.id !== id) throw new SessionPersistenceNotFoundError(id);
    if (access === 'read') return this.handle(record, access);
    const owner = this.acquire(id);
    try {
      const current = readRecord(this.root);
      if (current === undefined || current.header.id !== id) throw new SessionPersistenceNotFoundError(id);
      return this.writer(current, owner);
    }
    catch (error) { this.release(owner); throw error; }
  }
  async flush() {
    this.checkFailure();
    if (this.activeWriter !== undefined && !this.activeWriter.closed) await this.activeWriter.handle.flush();
  }
  async stat(id, options = {}) {
    checkSignal(options); this.checkFailure();
    if (this.expectedId !== undefined && this.expectedId !== id) return undefined;
    const record = readRecord(this.root);
    if (record === undefined || record.header.id !== id) return undefined;
    const file = statSync(this.eventsFile);
    return { header: structuredClone(record.header), inheritedEventCount: record.inheritedEventCount,
      revision: SessionPersistenceRevision(`${record.events.length}:${file.size}:${file.mtimeMs}`),
      eventCount: record.events.length, sizeBytes: file.size,
      location: { kind: FORMAT, path: this.eventsFile } };
  }
  async list(options = {}) {
    checkSignal(options); this.checkFailure();
    const record = readRecord(this.root);
    if (record === undefined) return [];
    const item = await this.stat(record.header.id, options);
    return item === undefined ? [] : [item];
  }
}

/** Mount the storage seam; the official agent loop owns all replay and repair. */
export async function persistencePlugin(ctx, config) {
  await ctx.plugin(DurableSessionPersistence, config);
  return ctx.sessionPersistence;
}
export default DurableSessionPersistence;
