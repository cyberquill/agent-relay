import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { join } from "node:path";
import { after, before, beforeEach, describe, test } from "node:test";

import * as plugin from "../agent-relay.ts";
import { AgentRelay } from "../agent-relay.ts";

const { decisionOf, encodeFrame, request, socketPath } = AgentRelay;

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Frame = { [key: string]: Json };

const dir = mkdtempSync("/tmp/ocp");
const sock = join(dir, "relay.sock");
const received: Frame[] = [];
let respond: (frame: Frame) => Json = () => ({});
let server: Server;

function readFrames(socket: Socket): void {
  let buffer = Buffer.alloc(0);
  socket.on("data", (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const size = buffer.readUInt32BE(0);
      if (buffer.length < 4 + size) return;
      const frame = JSON.parse(buffer.subarray(4, 4 + size).toString("utf8")) as Frame;
      buffer = buffer.subarray(4 + size);
      received.push(frame);
      socket.write(encodeFrame(respond(frame)));
    }
  });
}

before(async () => {
  server = createServer(readFrames);
  await new Promise<void>((resolve) => server.listen(sock, resolve));
  process.env.RELAY_SOCKET = sock;
});

after(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
  rmSync(dir, { recursive: true, force: true });
});

beforeEach(() => {
  received.length = 0;
  respond = () => ({});
  process.env.RELAY_SOCKET = sock;
});

describe("socketPath", () => {
  test("prefers RELAY_SOCKET, then RELAY_HOME", () => {
    assert.equal(socketPath({ RELAY_SOCKET: "/x.sock" }), "/x.sock");
    assert.equal(
      socketPath({ RELAY_HOME: "/h" }),
      "/h/Library/Application Support/agent-relay/relay.sock",
    );
  });
});

describe("framing", () => {
  test("4-byte big-endian length prefix", () => {
    const frame = encodeFrame({ a: "é" });
    assert.equal(frame.readUInt32BE(0), frame.length - 4);
    assert.deepEqual(JSON.parse(frame.subarray(4).toString("utf8")), { a: "é" });
  });

  test("round trip through relayd", async () => {
    respond = (frame) => ({ stdout: { echo: frame.op ?? null } });
    assert.deepEqual(await request({ op: "ping" }, sock), { stdout: { echo: "ping" } });
  });

  test("fails open when relayd is not running", async () => {
    const started = Date.now();
    assert.equal(await request({ op: "ping" }, join(dir, "missing.sock")), null);
    assert.ok(Date.now() - started < 1000);
  });
});

describe("decisionOf", () => {
  test("denies only on an explicit deny", () => {
    assert.deepEqual(decisionOf({ stdout: { decision: "deny", reason: "no" } }), { allow: false, reason: "no" });
    assert.equal(decisionOf({ stdout: { decision: "allow" } }).allow, true);
    assert.equal(decisionOf({ stdout: {} }).allow, true);
    assert.equal(decisionOf(null).allow, true);
  });

  test("denies when the connection was lost mid-request", () => {
    assert.deepEqual(decisionOf({ lost: true }), {
      allow: false,
      reason: "agent-relay connection lost; denied for safety",
    });
  });
});

describe("relayd restarting mid-approval", () => {
  let cut: Server;
  const cutSock = join(dir, "cut.sock");

  before(async () => {
    cut = createServer((socket) => {
      socket.once("data", () => socket.destroy());
    });
    await new Promise<void>((resolve) => cut.listen(cutSock, resolve));
  });

  after(async () => {
    await new Promise<void>((resolve) => cut.close(() => resolve()));
  });

  test("request reports a drop after connecting as lost, not as unreachable", async () => {
    assert.deepEqual(await request({ op: "ping" }, cutSock), { lost: true });
    assert.equal(await request({ op: "ping" }, join(dir, "missing.sock")), null);
  });

  test("tool.execute.before denies when the socket drops before a reply", async () => {
    const hooks = await AgentRelay({ directory: "/w" });
    process.env.RELAY_SOCKET = cutSock;
    await assert.rejects(
      hooks["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c" }, { args: {} }),
      /agent-relay connection lost; denied for safety/,
    );
  });

  test("tool.execute.before still allows when relayd is unreachable", async () => {
    process.env.RELAY_SOCKET = join(dir, "missing.sock");
    const hooks = await AgentRelay({ directory: "/w" });
    await hooks["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c" }, {});
  });
});

describe("plugin", () => {
  test("exports nothing but the plugin, since OpenCode runs every exported function as one", () => {
    const exported = Object.values(plugin);
    assert.ok(exported.length > 0);
    for (const value of exported) assert.equal(value, AgentRelay);
  });

  test("registers the server on load", async () => {
    await AgentRelay({ directory: "/w/proj", serverUrl: new URL("http://127.0.0.1:4096") });
    assert.equal(received[0]?.event, "plugin.register");
    assert.deepEqual((received[0]?.payload as Frame).serverUrl, "http://127.0.0.1:4096/");
    assert.equal((received[0]?.payload as Frame).directory, "/w/proj");
  });

  test("tool.execute.before throws on a phone deny and passes on allow", async () => {
    const hooks = await AgentRelay({ directory: "/w" });
    const call = { tool: "bash", sessionID: "ses_1", callID: "call_1" };
    respond = (frame) =>
      frame.event === "tool.execute.before" ? { stdout: { decision: "deny", reason: "Denied from the phone" } } : {};
    await assert.rejects(hooks["tool.execute.before"](call, { args: { command: "rm -rf x" } }), /Denied from the phone/);
    const sent = received.find((f) => f.event === "tool.execute.before");
    assert.deepEqual((sent?.payload as Frame).args, { command: "rm -rf x" });
    respond = () => ({ stdout: { decision: "allow" } });
    await hooks["tool.execute.before"](call, { args: {} });
  });

  test("tool.execute.after reports truncated output", async () => {
    const hooks = await AgentRelay({ directory: "/w" });
    await hooks["tool.execute.after"]({ tool: "bash", sessionID: "s", callID: "c" }, { output: "x".repeat(5000) });
    const sent = received.find((f) => f.event === "tool.execute.after");
    assert.equal(((sent?.payload as Frame).output as string).length, 2000);
  });

  test("works with relayd down", async () => {
    process.env.RELAY_SOCKET = join(dir, "missing.sock");
    const hooks = await AgentRelay({ directory: "/w" });
    await hooks["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c" }, {});
  });
});
