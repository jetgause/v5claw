# OpenClaw Super Center (v0 Pre-Build)

This repo is the **Bridge + Super Center UI** that keeps upstream codebases intact and plugs them in via providers.

Included in v0:
- Bridge (FastAPI) with: Provider registry, Run engine, Router (simple), Event stream (WS), Artifact vault (file upload), Projects (in-memory)
- Super Center UI (minimal workstation shell) at `/`
- Provider stubs:
  - `provider.openclaw.native` (stub)
  - `provider.puppeteer` (real puppeteer launch/goto/screenshot; others stub)
- Workspace bridges:
  - VS Code extension skeleton exposing `/invoke` for ide.* capabilities
- Embedded IDE: `code-server` container for a VSCodium-like embedded workstation module

## Quickstart (Docker Compose)

```bash
cd deploy/compose
docker compose up
```

Then open:
- Super Center UI: http://localhost:8080
- Embedded IDE (code-server): http://localhost:8443 (password: `openclaw`)

## Register providers

In UI, go to **15) Provider Hub** and register:
- `provider.openclaw.native` with base_url `http://localhost:5005`
- `provider.puppeteer` with base_url `http://localhost:5010`

Capabilities can be pasted comma-separated (v0 does not validate).

## Create a run + add steps

1) Home → Create Run
2) Runs → Add Step

Examples:
- capability: `browser.launch` with payload `{ "headless": true }` (provider `provider.puppeteer`)
- capability: `page.goto` with payload `{ "url": "https://example.com" }`
- capability: `page.screenshot` with payload `{ "url": "https://example.com" }`

The run will stream events in the Runs panel.

## VS Code workspace bridge

The extension lives in `workspace_bridges/vscode_extension`.

It starts a local HTTP server on `127.0.0.1:5020` exposing:
- `GET /health`
- `POST /invoke` for:
  - `ide.open_file`
  - `ide.apply_diff` (v0 structured edits)
  - `ide.run_command`
  - `ide.get_diagnostics`

To use it, run the extension in VS Code and register it as a provider in the Super Center UI:
- provider_id: `provider.vscode_hooks`
- base_url: `http://127.0.0.1:5020`
- capabilities: `ide.open_file,ide.apply_diff,ide.run_command,ide.get_diagnostics,bridge.status`

## Next wiring steps

- Implement adapters for Agent Zero / Kilo / Roo as providers (`providers/*_adapter`).
- Expand Router to support pins, scoring, multi-target parallelism, vote/race.
- Expand UI modules beyond v0 placeholder.



## VS Code Workspace Bridge Streaming
Set these env vars when running the VS Code extension bridge:
- OPENCLAW_BRIDGE_URL=http://localhost:8080
- OPENCLAW_PROVIDER_TOKEN=devtoken

