// [AC-17] relay-bridge message protocol, against a mocked vscode API and a fake relayd.
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import { Bridge, COMMANDS, modelList, openArgs } from "../src/bridge.ts";
import { asCommand, encodeFrame, FrameReader, MAX_FRAME, socketPath } from "../src/protocol.ts";
import { FakeRelayd, MockVsCode } from "./helpers.ts";

let relayd: FakeRelayd;
let api: MockVsCode;
let bridge: Bridge;

beforeEach(async () => {
  relayd = new FakeRelayd();
  await relayd.listen();
  api = new MockVsCode();
  bridge = new Bridge(api, { socketPath: relayd.path, version: "0.1.0", retryMs: [10, 40] });
});

afterEach(async () => {
  bridge.stop();
  await relayd.close();
});

async function connected(): Promise<void> {
  bridge.start();
  await relayd.accept();
  const pong = await relayd.call(0, "ping");
  assert.equal(pong.ok, true);
}

test("registers its window and folders on connect", async () => {
  bridge.start();
  const register = await relayd.accept();
  assert.deepEqual(register, {
    op: "bridge.register",
    protocol: 1,
    window_id: "window-1",
    folders: ["/work/alpha"],
    version: "0.1.0",
    vscode_version: "1.140.0",
  });
  await relayd.call(1, "ping");
  assert.equal(bridge.registered, true);
});

test("ping answers with the window id", async () => {
  await connected();
  assert.deepEqual(await relayd.call(7, "ping"), { id: 7, ok: true, result: { pong: true, window_id: "window-1" } });
});

test("models lists picker-visible models first", async () => {
  api.models = [
    { id: "hidden", name: "A hidden", vendor: "copilot", family: "x", isUserSelectable: false } as never,
    { id: "gpt", name: "GPT", vendor: "copilot", family: "gpt" },
    { id: "claude", name: "Claude", vendor: "copilot", family: "claude" },
  ];
  await connected();
  const reply = await relayd.call(2, "models");
  const ids = (reply.result as Array<{ id: string; picker: boolean }>).map((m) => [m.id, m.picker]);
  assert.deepEqual(ids, [
    ["claude", true],
    ["gpt", true],
    ["hidden", false],
  ]);
});

test("start opens a new Local chat then sends the prompt in agent mode", async () => {
  await connected();
  const reply = await relayd.call(3, "start", { query: "fix the bug", model: "claude" });
  assert.deepEqual(reply, { id: 3, ok: true, result: { started: true } });
  const done = await relayd.next();
  assert.deepEqual(done, { event: "turn.done" });
  assert.deepEqual(api.commands().filter((c) => c !== "ping"), [COMMANDS.newChat, COMMANDS.open]);
  assert.deepEqual(api.calls[1]?.args[0], {
    query: "fix the bug",
    mode: "agent",
    blockOnResponse: true,
    modelSelector: { id: "claude" },
  });
});

test("start replies before the turn finishes", async () => {
  api.blockOpen = true;
  await connected();
  const reply = await relayd.call(4, "start", { query: "long task" });
  assert.equal(reply.ok, true);
  assert.equal(api.pending.size, 1);
  api.pending.get(COMMANDS.open)?.();
  assert.deepEqual(await relayd.next(), { event: "turn.done" });
});

test("prompt keeps the desk draft and does not open a new chat", async () => {
  await connected();
  const reply = await relayd.call(5, "prompt", { query: "and the tests" });
  assert.deepEqual(reply.result, { sent: true });
  await relayd.next();
  assert.deepEqual(api.commands(), [COMMANDS.open]);
  assert.deepEqual(api.calls[0]?.args[0], {
    query: "and the tests",
    mode: "agent",
    blockOnResponse: true,
    preserveInput: true,
  });
});

test("cancel runs the chat command", async () => {
  await connected();
  assert.deepEqual(await relayd.call(10, "cancel"), { id: 10, ok: true, result: { done: true } });
  assert.deepEqual(api.commands(), [COMMANDS.cancel]);
});

test("tool approval commands are refused: they would act on whichever chat has focus", async () => {
  await connected();
  for (const [id, cmd] of [
    [11, "acceptTool"],
    [12, "skipTool"],
  ] as const) {
    assert.deepEqual(await relayd.call(id, cmd), { id, ok: false, error: `unknown command ${cmd}` });
  }
  assert.deepEqual(api.commands(), []);
});

test("an unknown command is an error reply, not a crash", async () => {
  await connected();
  assert.deepEqual(await relayd.call(20, "format-disk"), { id: 20, ok: false, error: "unknown command format-disk" });
  assert.equal((await relayd.call(21, "ping")).ok, true);
});

test("a failing command reports its error", async () => {
  api.failing.add(COMMANDS.cancel);
  await connected();
  const reply = await relayd.call(22, "cancel");
  assert.deepEqual(reply, { id: 22, ok: false, error: `${COMMANDS.cancel} failed` });
});

test("a failing chat turn is reported as an error event", async () => {
  api.failing.add(COMMANDS.open);
  await connected();
  assert.equal((await relayd.call(23, "prompt", { query: "x" })).ok, true);
  assert.deepEqual(await relayd.next(), { event: "error", message: `${COMMANDS.open} failed` });
});

test("folder changes are pushed as events", async () => {
  await connected();
  api.workspaceFolders = ["/work/alpha", "/work/beta"];
  bridge.foldersChanged();
  assert.deepEqual(await relayd.next(), { event: "folders", folders: ["/work/alpha", "/work/beta"] });
});

test("reconnects and re-registers after relayd drops the connection", async () => {
  await connected();
  relayd.dropClients();
  const again = await relayd.accept();
  assert.equal(again.op, "bridge.register");
  assert.equal(relayd.connections, 2);
  assert.equal((await relayd.call(30, "ping")).ok, true);
});

test("keeps retrying while relayd is down", async () => {
  await relayd.close();
  bridge.start();
  await new Promise((resolve) => setTimeout(resolve, 60));
  relayd = new FakeRelayd();
  const late = new Bridge(api, { socketPath: relayd.path, version: "0.1.0", retryMs: [10, 20] });
  late.start();
  await new Promise((resolve) => setTimeout(resolve, 30));
  await relayd.listen();
  const register = await relayd.accept();
  assert.equal(register.window_id, "window-1");
  late.stop();
});

test("frames split across chunks and packed together both decode", () => {
  const reader = new FrameReader();
  const a = encodeFrame({ id: 1, cmd: "ping" });
  const b = encodeFrame({ id: 2, cmd: "models", args: { x: 1 } });
  const both = Buffer.concat([a, b]);
  assert.deepEqual(reader.push(both.subarray(0, 3)), []);
  assert.deepEqual(reader.push(both.subarray(3, a.length + 2)), [{ id: 1, cmd: "ping" }]);
  assert.deepEqual(reader.push(both.subarray(a.length + 2)), [{ id: 2, cmd: "models", args: { x: 1 } }]);
});

test("oversized and non-object frames are rejected", () => {
  const huge = Buffer.alloc(4);
  huge.writeUInt32BE(MAX_FRAME + 1, 0);
  assert.throws(() => new FrameReader().push(huge), /too large/);
  const list = Buffer.from(JSON.stringify([1]));
  const header = Buffer.alloc(4);
  header.writeUInt32BE(list.length, 0);
  assert.throws(() => new FrameReader().push(Buffer.concat([header, list])), /not a JSON object/);
});

test("a bad frame from relayd drops the connection and reconnects", async () => {
  await connected();
  const header = Buffer.alloc(4);
  header.writeUInt32BE(MAX_FRAME + 1, 0);
  relayd.sendRaw(header);
  const again = await relayd.accept();
  assert.equal(again.op, "bridge.register");
});

test("asCommand needs a numeric id and a command name", () => {
  assert.deepEqual(asCommand({ id: 1, cmd: "ping" }), { id: 1, cmd: "ping", args: {} });
  assert.equal(asCommand({ cmd: "ping" }), null);
  assert.equal(asCommand({ id: "1", cmd: "ping" }), null);
  assert.deepEqual(asCommand({ id: 1, cmd: "x", args: [1] }), { id: 1, cmd: "x", args: {} });
});

test("socket path honours the setting, RELAY_SOCKET, then RELAY_HOME", () => {
  assert.equal(socketPath({ RELAY_SOCKET: "/s" }, "/override"), "/override");
  assert.equal(socketPath({ RELAY_SOCKET: "/s", RELAY_HOME: "/h" }), "/s");
  assert.equal(socketPath({ RELAY_HOME: "/h" }), "/h/Library/Application Support/agent-relay/relay.sock");
});

test("openArgs defaults to agent mode and omits an empty model", () => {
  assert.deepEqual(openArgs({ query: "q", model: "" }, false), { query: "q", mode: "agent", blockOnResponse: true });
  assert.deepEqual(openArgs({ query: "q", mode: "ask" }, false).mode, "ask");
  assert.deepEqual(modelList([]), []);
});
