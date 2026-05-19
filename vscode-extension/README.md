# Artheia VS Code Extension

Language support for Artheia `.art` files:
- TextMate grammar for highlighting (works without the language server).
- LSP client that talks to `artheia-lsp` for diagnostics, go-to-definition,
  and completion.

## Install

1. Install the language server into a Python environment:

   ```sh
   pip install -e ../    # from the artheia repo root
   ```

   Confirm `artheia-lsp` is on `PATH` (or set `artheia.serverCommand` to
   its absolute path in VS Code settings).

2. Build and install this extension:

   ```sh
   cd vscode-extension
   npm install
   npm run compile
   npx vsce package --no-dependencies
   code --install-extension artheia-*.vsix
   ```

## Settings

- `artheia.serverCommand` — command to launch the language server (default:
  `artheia-lsp`).
- `artheia.serverArgs` — extra args passed to the language server.

## Workspace conventions

- The LSP looks for `*catalog*.json` files anywhere in the workspace to
  populate completion for gateway/AUTOSAR messages.
- Generate one with `artheia import-dbc --dbc ... --bus ... --out vendor/autosar/<bus>/`
  (or `import-fibex` for FlexRay). Each writes a `catalog.json` next to a `package.art`.
