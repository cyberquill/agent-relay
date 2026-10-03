// Test doubles: a fake relayd on a temp Unix socket, and a mocked vscode API that records commands.
import { mkdtempSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ChatModel, VsCodeApi } from "../src/bridge.ts";
import { encodeFrame, FrameReader, type JsonObject } from "../src/protocol.ts";

export class FakeRelayd {
  readonly path: string;
  private server: Server;
  private clients: Socket[] = [];
  private frames: JsonObject[] = [];
  private waiters: Array<(frame: JsonObject) => void> = [];
  connections = 0;

  constructor() {
    this.path = join(mkdtempSync(join(tmpdir(), "relay-bridge-")), "relay.sock");
    this.server = createServer((socket) => {
      this.connections += 1;
      this.clients.push(socket);
      const reader = new FrameReader();
      socket.on("data", (chunk: Buffer) => {
        for (const frame of reader.push(chunk)) this.deliver(frame);
      });
      socket.on("error", () => undefined);
    });
  }

  private deliver(frame: JsonObject): void {
    const waiter = this.waiters.shift();
    if (waiter) waiter(frame);
    else this.frames.push(frame);
  }

  listen(): Promise<void> {
    return new Promise((resolve) => this.server.listen(this.path, () => resolve()));
  }

  next(timeoutMs = 2000): Promise<JsonObject> {
    const ready = this.frames.shift();
    if (ready) return Promise.resolve(ready);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("no frame from bridge")), timeoutMs);
      this.waiters.push((frame) => {
        clearTimeout(timer);
        resolve(frame);
      });
    });
  }

  send(frame: JsonObject): void {
    this.sendRaw(encodeFrame(frame));
  }

  sendRaw(bytes: Buffer): void {
    const client = this.clients.at(-1);
    if (!client) throw new Error("no bridge connected");
    client.write(bytes);
  }

  async accept(): Promise<JsonObject> {
    const register = await this.next();
    this.send({ ok: true, protocol: 1 });
    return register;
  }

  async call(id: number, cmd: string, args: JsonObject = {}): Promise<JsonObject> {
    this.send({ id, cmd, args });
    return this.next();
  }

  dropClients(): void {
    for (const client of this.clients) client.destroy();
    this.clients = [];
  }

  close(): Promise<void> {
    this.dropClients();
    return new Promise((resolve) => this.server.close(() => resolve()));
  }
}

export type Call = { command: string; args: unknown[] };

export class MockVsCode implements VsCodeApi {
  readonly windowId = "window-1";
  readonly version = "1.140.0";
  calls: Call[] = [];
  workspaceFolders: string[] = ["/work/alpha"];
  models: ChatModel[] = [];
  failing = new Set<string>();
  pending = new Map<string, () => void>();
  blockOpen = false;

  executeCommand(command: string, ...args: unknown[]): Promise<unknown> {
    this.calls.push({ command, args });
    if (this.failing.has(command)) return Promise.reject(new Error(`${command} failed`));
    if (this.blockOpen && command === "workbench.action.chat.open") {
      return new Promise((resolve) => this.pending.set(command, () => resolve(undefined)));
    }
    return Promise.resolve(undefined);
  }

  selectChatModels(): Promise<readonly ChatModel[]> {
    return Promise.resolve(this.models);
  }

  folders(): string[] {
    return this.workspaceFolders;
  }

  commands(): string[] {
    return this.calls.map((c) => c.command);
  }
}
