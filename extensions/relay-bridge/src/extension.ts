// VS Code entry point: wires the real vscode API into the Bridge and keeps it running for the
// lifetime of the window.
import * as vscode from "vscode";
import { Bridge, type ChatModel, type VsCodeApi } from "./bridge.ts";
import { socketPath } from "./protocol.ts";

let bridge: Bridge | null = null;

export function vscodeApi(): VsCodeApi {
  return {
    windowId: vscode.env.sessionId,
    version: vscode.version,
    executeCommand: (command: string, ...args: unknown[]) => vscode.commands.executeCommand(command, ...args),
    selectChatModels: async (): Promise<readonly ChatModel[]> => vscode.lm.selectChatModels({}),
    folders: () => (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath),
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Agent Relay Bridge");
  const override = vscode.workspace.getConfiguration("relayBridge").get<string>("socketPath", "");
  const version = String((context.extension.packageJSON as { version?: string }).version ?? "0.0.0");
  bridge = new Bridge(vscodeApi(), {
    socketPath: socketPath(process.env, override),
    version,
    log: { info: (m) => output.appendLine(m), error: (m) => output.appendLine(m) },
  });
  bridge.start();
  context.subscriptions.push(
    output,
    vscode.workspace.onDidChangeWorkspaceFolders(() => bridge?.foldersChanged()),
    { dispose: () => bridge?.stop() },
  );
}

export function deactivate(): void {
  bridge?.stop();
  bridge = null;
}
