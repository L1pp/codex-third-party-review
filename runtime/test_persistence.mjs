/** Storage-boundary regressions; no model, shell, native addon or network. */
import assert from 'node:assert/strict';
import { closeSync, existsSync, readFileSync, readdirSync, appendFileSync, writeFileSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { hostname, tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import SessionStore, { Session, SessionId } from '@deepseek-ai/dsh-session';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import {
  SessionAlreadyExistsError, SessionAlreadyOwnedError, SessionHandleClosedError,
  SessionOwnershipLostError, SessionPersistenceCorruptionError,
  SessionPersistenceNotFoundError, SessionReadOnlyError,
} from '@deepseek-ai/dsh-session-persistence';
import DurableSessionPersistence, { persistencePlugin } from './persistence.mjs';

function fixture(id = 'fixture-review', count = 2) {
  const session = Session.create(SessionId(id));
  for (let i = 0; i < count; i++) {
    session.append('user/message', createUserMessage({ source: { kind: 'user' },
      content: [{ type: 'text', text: `Review source range ${i + 1}.` }] }), { surfaceOp: 'append' });
  }
  return { id: SessionId(id), header: session.header, events: session.snapshotEvents() };
}
async function environment(t, config = {}) {
  const home = await mkdtemp(join(tmpdir(), 'review-persistence-'));
  const root = config.root ?? join(home, 'session');
  const ctx = new Context();
  await ctx.plugin(SessionStore);
  const service = await persistencePlugin(ctx, { root, ...config });
  t.after(async () => {
    try { await ctx.root.fiber.dispose(); } catch { /* A test may intentionally poison the writer. */ }
    await rm(home, { recursive: true, force: true });
  });
  return { home, root, ctx, service };
}
function bytesUnder(root) {
  return Object.fromEntries(readdirSync(root).filter(name => !name.includes('writer.lock'))
    .map(name => [name, readFileSync(join(root, name))]));
}

test('create, append, flush, slice and restart use official event validation', async t => {
  const { root, service, ctx } = await environment(t);
  const f = fixture();
  const handle = await service.create(f.header);
  assert.equal(handle.access, 'write');
  await handle.append(f.events);
  await handle.flush();
  const slice = await handle.read(1, 1);
  assert.equal(slice.eventState, 'detached');
  assert.deepEqual(slice.events, f.events.slice(1));
  slice.events[0].data.content[0].text = 'caller-owned edit';
  assert.deepEqual((await handle.read()).events, f.events);
  assert.equal((await service.stat(f.id)).eventCount, 2);
  assert.equal((await service.list()).length, 1);
  await handle.close();
  await ctx.root.fiber.dispose();
  const next = new Context();
  await next.plugin(SessionStore);
  const reopened = await persistencePlugin(next, { root });
  const reader = await reopened.open(f.id, 'read');
  assert.deepEqual((await reader.read()).events, f.events);
  await reader.close();
  await next.root.fiber.dispose();
});

test('the plugin routes live session events and flush makes them visible to a new backend', async t => {
  const { root, service, ctx } = await environment(t);
  const session = ctx.sessions.create(SessionId('live'));
  const writer = await service.create(session.header);
  session.append('user/message', createUserMessage({ source: { kind: 'user' },
    content: [{ type: 'text', text: 'Live material already admitted.' }] }), { surfaceOp: 'append' });
  await service.flush();
  const second = new Context();
  const other = await persistencePlugin(second, { root });
  const reader = await other.open(session.id, 'read');
  assert.equal((await reader.read()).events.length, 1);
  await reader.close(); await second.root.fiber.dispose(); await writer.close();
});

test('one writer owns the selected directory while fresh read handles see appends', async t => {
  const { root, service } = await environment(t);
  const f = fixture(); const writer = await service.create(f.header);
  const otherCtx = new Context(); const other = await persistencePlugin(otherCtx, { root });
  await assert.rejects(other.open(f.id, 'write'), SessionAlreadyOwnedError);
  const reader = await other.open(f.id, 'read');
  assert.equal((await reader.read()).events.length, 0);
  await writer.append(f.events); await writer.flush();
  assert.equal((await reader.read()).events.length, 2);
  await assert.rejects(reader.append([]), SessionReadOnlyError);
  await assert.rejects(reader.flush(), SessionReadOnlyError);
  await reader.close(); await otherCtx.root.fiber.dispose(); await writer.close();
  assert.equal(existsSync(join(root, 'writer.lock')), false);
});

test('create never overwrites a previous session and missing ids stay missing', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  const original = bytesUnder(root);
  await assert.rejects(service.create(f.header), SessionAlreadyExistsError);
  await assert.rejects(service.open(SessionId('absent'), 'read'), SessionPersistenceNotFoundError);
  assert.deepEqual(bytesUnder(root), original);
});

test('invalid append sequence rejects without poisoning the intact writer', async t => {
  const { service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header);
  await assert.rejects(writer.append([f.events[1]]), /append seq mismatch/);
  assert.equal((await writer.read()).events.length, 0);
  await writer.append(f.events); await writer.close();
});

test('cancellation is observed before create, append and flush begin', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const abort = AbortSignal.abort(new Error('cancelled before I/O'));
  await assert.rejects(service.create(f.header, { signal: abort }), /cancelled/);
  assert.deepEqual(readdirSync(root), []);
  const writer = await service.create(f.header);
  await assert.rejects(writer.append(f.events, { signal: abort }), /cancelled/);
  await assert.rejects(writer.flush({ signal: abort }), /cancelled/);
  assert.equal((await writer.read()).events.length, 0);
  await writer.close();
  await assert.rejects(writer.read(), SessionHandleClosedError);
  await writer.close();
});

test('a torn physical tail is not returned and is retained before writable repair', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  const tail = Buffer.from('{"unfinished":"partial UTF8 ');
  appendFileSync(join(root, 'session.jsonl'), Buffer.concat([tail, Buffer.from([0xe4, 0xb8])]));
  const reader = await service.open(f.id, 'read');
  assert.deepEqual((await reader.read()).events, f.events); await reader.close();
  const repair = await service.open(f.id, 'write');
  const backups = readdirSync(root).filter(name => name.startsWith('session-tail-'));
  assert.equal(backups.length, 1);
  assert.deepEqual(readFileSync(join(root, backups[0])), Buffer.concat([tail, Buffer.from([0xe4, 0xb8])]));
  await repair.close();
  assert.ok(readFileSync(join(root, 'session.jsonl')).at(-1) === 0x0a);
});

test('malformed complete or middle records fail closed rather than dropping events', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  appendFileSync(join(root, 'session.jsonl'), '{not-json}\n');
  await assert.rejects(service.open(f.id, 'read'), SessionPersistenceCorruptionError);
  const raw = readFileSync(join(root, 'session.jsonl'));
  assert.ok(raw.includes(Buffer.from('{not-json}')));
});

test('seeded resume copies valid recorded events into a new root without modifying old files', async t => {
  const { root, service, home } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  const original = bytesUnder(root);
  const newCtx = new Context(); const destination = join(home, 'resumed-session');
  const seeded = await persistencePlugin(newCtx, { root: destination, seedDirectory: root, sessionId: f.id });
  assert.equal(seeded.seeded, true);
  const resumed = await seeded.open(f.id, 'write');
  assert.deepEqual((await resumed.read()).events, f.events);
  await resumed.close(); await newCtx.root.fiber.dispose();
  assert.deepEqual(bytesUnder(root), original);
  assert.equal(JSON.parse(readFileSync(join(destination, 'seed-info.json'), 'utf8')).eventCount, 2);
});

test('seed excludes only an uncommitted physical tail and leaves its old bytes intact', async t => {
  const { root, service, home } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  appendFileSync(join(root, 'session.jsonl'), '{partial');
  const old = readFileSync(join(root, 'session.jsonl'));
  const next = new Context(); const destination = join(home, 'resumed');
  const seeded = await persistencePlugin(next, { root: destination, seedDirectory: root });
  const reader = await seeded.open(f.id, 'read'); assert.deepEqual((await reader.read()).events, f.events);
  await reader.close(); await next.root.fiber.dispose();
  assert.deepEqual(readFileSync(join(root, 'session.jsonl')), old);
  assert.equal(JSON.parse(readFileSync(join(destination, 'seed-info.json'), 'utf8')).omittedPhysicalTailBytes, 8);
});

test('explicit new-root resume accepts a same-host dead writer record without changing it', async t => {
  const deadPid = 2147483647;
  try { process.kill(deadPid, 0); t.skip('Selected diagnostic PID exists'); return; }
  catch (error) { if (error.code !== 'ESRCH') { t.skip('Cannot confirm a dead PID on this host'); return; } }
  const { root, service, home } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events); await writer.close();
  const stale = JSON.stringify({ pid: deadPid, host: hostname(), token: 'old-crashed-writer' });
  writeFileSync(join(root, 'writer.lock'), stale);
  const next = new Context(); const destination = join(home, 'after-crash');
  const seeded = await persistencePlugin(next, { root: destination, seedDirectory: root, sessionId: f.id });
  const reader = await seeded.open(f.id, 'read'); assert.equal((await reader.read()).events.length, 2);
  assert.equal(await seeded.stat(SessionId('not-selected')), undefined);
  await reader.close(); await next.root.fiber.dispose();
  assert.equal(readFileSync(join(root, 'writer.lock'), 'utf8'), stale);
});

test('seed refuses active writers and overlapping output roots before polluting the old run', async t => {
  const { root, service, home } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.append(f.events);
  const second = new Context();
  await assert.rejects(persistencePlugin(second, { root: join(home, 'seed-live'), seedDirectory: root }), SessionAlreadyOwnedError);
  await second.root.fiber.dispose();
  await writer.close();
  const original = bytesUnder(root);
  for (const destination of [root, join(root, 'must-not-be-created'), home]) {
    const child = new Context();
    await assert.rejects(persistencePlugin(child, { root: destination, seedDirectory: root }), /separate output/);
    await child.root.fiber.dispose();
  }
  assert.deepEqual(bytesUnder(root), original);
  assert.equal(existsSync(join(root, 'must-not-be-created')), false);
});

test('unknown newer required events and unsupported storage versions are refused', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header); await writer.close();
  appendFileSync(join(root, 'session.jsonl'), JSON.stringify({ seq: 0, time: Date.now(), type: 'future/required', data: {} }) + '\n');
  await assert.rejects(service.open(f.id, 'read'), /unknown|newer/i);
  writeFileSync(join(root, 'session.jsonl'), '');
  const meta = JSON.parse(readFileSync(join(root, 'session-header.json'), 'utf8'));
  meta.formatVersion = 99; writeFileSync(join(root, 'session-header.json'), JSON.stringify(meta));
  await assert.rejects(service.open(f.id, 'read'), /Unsupported review session storage format/);
});

test('lost writer identity prevents append and close never deletes the new owner', async t => {
  const { root, service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header);
  const ownerPath = join(root, 'writer.lock'); const owner = JSON.parse(readFileSync(ownerPath, 'utf8'));
  const replacement = { ...owner, token: 'different-independent-owner' };
  writeFileSync(ownerPath, JSON.stringify(replacement));
  await assert.rejects(writer.append(f.events), SessionOwnershipLostError);
  await assert.rejects(writer.close(), SessionOwnershipLostError);
  assert.equal(JSON.parse(readFileSync(ownerPath, 'utf8')).token, replacement.token);
});

test('physical append failure is latched and fails the next API durability barrier', async t => {
  const { service } = await environment(t); const f = fixture();
  const writer = await service.create(f.header);
  closeSync(service.activeWriter.fd);
  await assert.rejects(writer.append(f.events), /EBADF/);
  await assert.rejects(service.flush(), /EBADF/);
  await assert.rejects(writer.read(), /EBADF/);
  await assert.rejects(writer.close(), /EBADF/);
});
