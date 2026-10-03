// agent-relay OpenCode plugin: forwards tool calls to relayd for phone approval while away mode
// is on, and registers this OpenCode server with the relay. Fails open when relayd is down.
import { connect } from "node:net";
import { homedir } from "node:os";
import { join } from "node:path";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Reply = { stdout?: Json; exit?: number };

export function socketPath(env: Record<string, string | undefined> = process.env): string {
  if (env.RELAY_SOCKET) return env.RELAY_SOCKET;
  const home = env.RELAY_HOME ?? homedir();
  return join(home, "Library", "Application Support", "agent-relay", "relay.sock");
}

export function encodeFrame(message: Json): Buffer {
  const body = Buffer.from(JSON.stringify(message), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length, 0);
  return Buffer.concat([header, body]);
}

export function request(message: Json, sock: string = socketPath(), connectTimeoutMs = 200): Promise<Reply | null> {
  return new Promise((resolve) => {
    let settled = false;
    let buffer = Buffer.alloc(0);
    const finish = (value: Reply | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(value);
    };
    const socket = connect(sock);
    const timer = setTimeout(() => finish(null), connectTimeoutMs);
    socket.on("connect", () => {
      clearTimeout(timer);
      socket.write(encodeFrame(message));
    });
    socket.on("data", (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length < 4) return;
      const size = buffer.readUInt32BE(0);
      if (buffer.length < 4 + size) return;
      try {
        finish(JSON.parse(buffer.subarray(4, 4 + size).toString("utf8")) as Reply);
      } catch {
        finish(null);
      }
    });
    socket.on("error", () => finish(null));
    socket.on("close", () => finish(null));
  });
}

export function hook(event: string, payload: { [key: string]: Json }, sock?: string): Promise<Reply | null> {
  return request({ op: "hook", harness: "opencode", event, payload }, sock);
}

export function decisionOf(reply: Reply | null): { allow: boolean; reason: string } {
  const out = reply?.stdout;
  if (out && typeof out === "object" && !Array.isArray(out) && out.decision === "deny") {
    const reason = typeof out.reason === "string" ? out.reason : "Denied from the phone";
    return { allow: false, reason };
  }
  return { allow: true, reason: "" };
}

type PluginInput = { directory?: string; worktree?: string; serverUrl?: URL | string };
type ToolInput = { tool: string; sessionID: string; callID: string };
type ToolOutput = { args?: Json; title?: string; output?: string };

export const AgentRelay = async (input: PluginInput) => {
  const directory = input.directory ?? input.worktree ?? process.cwd();
  const serverUrl = input.serverUrl ? String(input.serverUrl) : "";
  await hook("plugin.register", { directory, serverUrl, pid: process.pid });
  return {
    "tool.execute.before": async (tool: ToolInput, output: ToolOutput): Promise<void> => {
      const reply = await hook("tool.execute.before", {
        sessionID: tool.sessionID,
        callID: tool.callID,
        tool: tool.tool,
        args: output.args ?? null,
        directory,
      });
      const decision = decisionOf(reply);
      if (!decision.allow) throw new Error(decision.reason);
    },
    "tool.execute.after": async (tool: ToolInput, output: ToolOutput): Promise<void> => {
      await hook("tool.execute.after", {
        sessionID: tool.sessionID,
        callID: tool.callID,
        tool: tool.tool,
        output: typeof output.output === "string" ? output.output.slice(0, 2000) : null,
        directory,
      });
    },
  };
};

export default AgentRelay;
