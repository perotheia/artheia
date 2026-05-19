import * as path from 'path';
import {
  workspace,
  ExtensionContext,
  window,
} from 'vscode';
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
  TransportKind,
} from 'vscode-languageclient/node';

let client: LanguageClient | undefined;

export function activate(context: ExtensionContext): void {
  const config = workspace.getConfiguration('artheia');
  const command = config.get<string>('serverCommand', 'artheia-lsp');
  const args = config.get<string[]>('serverArgs', []);

  const serverOptions: ServerOptions = {
    run:   { command, args, transport: TransportKind.stdio },
    debug: { command, args, transport: TransportKind.stdio },
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: 'file', language: 'artheia' }],
    synchronize: {
      fileEvents: [
        workspace.createFileSystemWatcher('**/*.art'),
        workspace.createFileSystemWatcher('**/*catalog*.json'),
      ],
    },
    outputChannelName: 'Artheia',
  };

  client = new LanguageClient(
    'artheia',
    'Artheia Language Server',
    serverOptions,
    clientOptions,
  );

  client.start().catch((err: Error) => {
    window.showErrorMessage(
      `Artheia LSP failed to start (command "${command}"): ${err.message}. ` +
      `Install the language server with: pip install -e /path/to/artheia`,
    );
  });
}

export function deactivate(): Thenable<void> | undefined {
  if (!client) return undefined;
  return client.stop();
}
