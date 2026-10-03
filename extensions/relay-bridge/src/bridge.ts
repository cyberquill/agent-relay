// The bridge: one long-lived connection from this VS Code window to relayd. relayd sends commands
// ({id, cmd, args}); the bridge runs them through the injected VS Code API and replies {id, ok, ...}.
import { connect, type Socket } from "node:net";
import { asCommand, encodeFrame, FrameReader, PROTOCOL, type Command, type Json, type JsonObject } from "./protocol.ts";

export const COMMANDS = {
  newChat: "workbench.action.chat.newLocalChat",
  open: "workbench.action.chat.open",
  cancel: "workbench.action.chat.cancel",
  accept: "workbench.action.chat.acceptTool",
  skip: "workbench.action.chat.skipTool",
} as const;

export interface ChatModel {
  readonly id: string;
  readonly name: string;
  readonly vendor: string;
  readonly family: string;
}

export interface VsCodeApi {
  readonly windowId: string;
  readonly version: string;
  executeCommand(command: string, ...args: unknown[]): PromiseLike<unknown>;
  selectChatModels(): PromiseLike<readonly ChatModel[]>;
  folders(): string[];
}

export interface Logger {
  info(message: string): void;
  error(message: string): void;
}

export interface BridgeOptions {
  socketPath: string;
  version: string;
  connect?: (path: string) => Socket;
  retryMs?: readonly [number, number];
  log?: Logger;
}

const quiet: Logger = { info: () => undefined, error: () => undefined };

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function selectable(model: ChatModel): boolean {
  const flag: unknown = (model as unknown as Record<string, unknown>)["isUserSelectable"];
  return flag !== false;
}

export function modelList(models: readonly ChatModel[]): JsonObject[] {
  const ranked = [...models].sort((a, b) => {
    const pick = Number(selectable(b)) - Number(selectable(a));
    return pick !== 0 ? pick : a.name.localeCompare(b.name);
  });
  return ranked.map((m) => ({ id: m.id, label: m.name, vendor: m.vendor, family: m.family, picker: selectable(m) }));
}

// `keepDraft` keeps whatever the user typed at the desk in the input box (phone prompts into an
// existing chat); a fresh chat from the phone has no draft to keep.
export function openArgs(args: JsonObject, keepDraft: boolean): JsonObject {
  const query = typeof args.query === "string" ? args.query : "";
  const mode = typeof args.mode === "string" ? args.mode : "agent";
  const open: JsonObject = { query, mode, blockOnResponse: true };
  if (typeof args.model === "string" && args.model) open.modelSelector = { id: args.model };
  if (keepDraft) open.preserveInput = true;
  return open;
}

export class Bridge {
  private socket: Socket | null = null;
  private reader = new FrameReader();
  private stopped = false;
  private delay: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly api: VsCodeApi;
  private readonly options: BridgeOptions;
  registered = false;

  constructor(api: VsCodeApi, options: BridgeOptions) {
    this.api = api;
    this.options = options;
    this.delay = options.retryMs?.[0] ?? 1000;
  }

  private get log(): Logger {
    return this.options.log ?? quiet;
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.socket?.destroy();
    this.socket = null;
    this.registered = false;
  }

  private open(): void {
    const socket = (this.options.connect ?? connect)(this.options.socketPath);
    this.socket = socket;
    this.reader = new FrameReader();
    socket.on("connect", () => {
      this.send({
        op: "bridge.register",
        protocol: PROTOCOL,
        window_id: this.api.windowId,
        folders: this.api.folders(),
        version: this.options.version,
        vscode_version: this.api.version,
      });
    });
    socket.on("data", (chunk: Buffer) => this.receive(chunk));
    socket.on("error", (err: Error) => this.log.info(`relay-bridge: ${err.message}`));
    socket.on("close", () => this.closed(socket));
  }

  private closed(socket: Socket): void {
    if (this.socket !== socket) return;
    this.socket = null;
    this.registered = false;
    if (this.stopped) return;
    const [, high] = this.options.retryMs ?? [1000, 30000];
    const wait = this.delay;
    this.delay = Math.min(this.delay * 2, high);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.stopped) this.open();
    }, wait);
  }

  private receive(chunk: Buffer): void {
    let frames: JsonObject[];
    try {
      frames = this.reader.push(chunk);
    } catch (err) {
      this.log.error(`relay-bridge: bad frame from relayd: ${errorText(err)}`);
      this.socket?.destroy();
      return;
    }
    for (const frame of frames) {
      if (!this.registered && frame.ok === true && frame.id === undefined) {
        this.registered = true;
        this.delay = this.options.retryMs?.[0] ?? 1000;
        this.log.info("relay-bridge: registered with relayd");
        continue;
      }
      const command = asCommand(frame);
      if (command) void this.reply(command);
    }
  }

  private async reply(command: Command): Promise<void> {
    const followUps: Array<() => void> = [];
    try {
      const result = await this.handle(command, followUps);
      this.send({ id: command.id, ok: true, result });
    } catch (err) {
      this.send({ id: command.id, ok: false, error: errorText(err) });
      return;
    }
    for (const run of followUps) run();
  }

  send(message: JsonObject): void {
    if (this.socket && !this.socket.destroyed) this.socket.write(encodeFrame(message));
  }

  foldersChanged(): void {
    if (this.registered) this.send({ event: "folders", folders: this.api.folders() });
  }

  // A chat turn blocks until the response finishes, so it runs after the reply has gone out and
  // reports its end (or failure) as an event.
  private turn(args: JsonObject): () => void {
    return () => {
      Promise.resolve(this.api.executeCommand(COMMANDS.open, args)).then(
        () => this.send({ event: "turn.done" }),
        (err: unknown) => this.send({ event: "error", message: errorText(err) }),
      );
    };
  }

  async handle(command: Command, followUps: Array<() => void> = []): Promise<Json> {
    switch (command.cmd) {
      case "ping":
        return { pong: true, window_id: this.api.windowId };
      case "models":
        return modelList(await this.api.selectChatModels());
      case "start":
        await this.api.executeCommand(COMMANDS.newChat);
        followUps.push(this.turn(openArgs(command.args, false)));
        return { started: true };
      case "prompt":
        followUps.push(this.turn(openArgs(command.args, true)));
        return { sent: true };
      case "cancel":
        await this.api.executeCommand(COMMANDS.cancel);
        return { done: true };
      case "acceptTool":
        await this.api.executeCommand(COMMANDS.accept);
        return { done: true };
      case "skipTool":
        await this.api.executeCommand(COMMANDS.skip);
        return { done: true };
      default:
        throw new Error(`unknown command ${command.cmd}`);
    }
  }
}
