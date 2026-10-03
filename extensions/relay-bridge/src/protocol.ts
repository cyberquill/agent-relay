// Frames on relay.sock: a 4-byte big-endian length, then that many bytes of UTF-8 JSON (one object).
import { homedir } from "node:os";
import { join } from "node:path";

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export const MAX_FRAME = 16 * 1024 * 1024;
export const PROTOCOL = 1;

export function socketPath(env: Record<string, string | undefined> = process.env, override = ""): string {
  if (override) return override;
  if (env.RELAY_SOCKET) return env.RELAY_SOCKET;
  const home = env.RELAY_HOME ?? homedir();
  return join(home, "Library", "Application Support", "agent-relay", "relay.sock");
}

export function encodeFrame(message: JsonObject): Buffer {
  const body = Buffer.from(JSON.stringify(message), "utf8");
  if (body.length > MAX_FRAME) throw new Error(`frame too large: ${body.length} bytes`);
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length, 0);
  return Buffer.concat([header, body]);
}

export class FrameReader {
  private buffer: Buffer = Buffer.alloc(0);

  // Feed raw bytes; returns every complete frame. Throws on an oversized or non-object frame.
  push(chunk: Buffer): JsonObject[] {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    const frames: JsonObject[] = [];
    while (this.buffer.length >= 4) {
      const size = this.buffer.readUInt32BE(0);
      if (size > MAX_FRAME) throw new Error(`frame too large: ${size} bytes`);
      if (this.buffer.length < 4 + size) break;
      const value: unknown = JSON.parse(this.buffer.subarray(4, 4 + size).toString("utf8"));
      this.buffer = this.buffer.subarray(4 + size);
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("frame is not a JSON object");
      }
      frames.push(value as JsonObject);
    }
    return frames;
  }
}

export type Command = { id: number; cmd: string; args: JsonObject };

export function asCommand(frame: JsonObject): Command | null {
  const { id, cmd, args } = frame;
  if (typeof id !== "number" || typeof cmd !== "string") return null;
  const parsed = args !== null && typeof args === "object" && !Array.isArray(args) ? args : {};
  return { id, cmd, args: parsed };
}
