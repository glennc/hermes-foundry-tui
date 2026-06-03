# Hermes Foundry TUI

PoC integration repo for running the existing Hermes React/Ink TUI against a Hermes agent hosted through Azure AI Foundry.

This repo intentionally stays separate from Hermes. Hermes source is pinned as a Git submodule under `third_party/hermes`, while this repo owns azd infrastructure, deployment glue, local configuration, and integration scaffolding.

The idea is to evolve this into a fully working solution that people can pick up, but it isn't that yet. Help us build it, don't expect it to be easy yet.

## Repository shape

```text
.
├── agent/                # azd AI Agents hosted-agent project
├── infra/                # azd AI Foundry starter infrastructure
├── scripts/              # Local setup and azd helper scripts
├── third_party/          # Hermes source submodule
├── azure.yaml
└── PROJECT_BRIEF.md
```

## Prerequisites

- Azure CLI (`az`) and Azure Developer CLI (`azd`)
- Azure Developer CLI AI agent extension: `azd extension install azure.ai.agents`
- Python 3.11 or newer plus `uv` for the Hermes backend environment
- Node.js and npm for the React/Ink TUI
- Azure quota for the default `gpt-5.4` and `gpt-5.4-mini` DataZone Standard deployments in `westus2`, or custom model deployment settings before provisioning

## Initial setup

```bash
git clone --recurse-submodules <repo-url>
cd hermes-foundry-tui
./scripts/init-hermes.sh
```

If the repo was cloned without submodules, `./scripts/init-hermes.sh` will fetch `third_party/hermes`.

Hermes changes for this PoC live on the `foundry-tui-poc-clean` branch of `https://github.com/glennc/hermes-agent.git`.

## Local agent testing

Use the Azure Developer CLI AI agent extension for local development:

```bash
azd extension install azure.ai.agents
azd auth login
azd provision
azd ai agent run
```

In another terminal:

```bash
cat > /tmp/hermes-rpc-setup.json <<'JSON'
{"kind":"hermes.rpc","request":{"jsonrpc":"2.0","id":"setup","method":"setup.status","params":{}},"session":{"id":"","workspace":"local-smoke"},"tui":{"protocol_version":1}}
JSON
azd ai agent invoke --local --protocol invocations -f /tmp/hermes-rpc-setup.json
```

The local agent starts on port `8088` by default. Invocations now accept only the `hermes.rpc` protocol used by the TUI path; direct text invokes are intentionally rejected so they do not mask real Hermes behavior.

`azd provision` creates the Foundry project and default OpenAI-family model deployments for the dev loop. The primary deployment is `gpt-5.4` in `chat_completions` mode with `auth_mode=entra_id`, so local runs use your Azure developer identity and hosted runs use the agent identity instead of API keys. The auxiliary deployment is `gpt-5.4-mini`.

Hosted-agent infrastructure is enabled by default so `azd up` can deploy the remote agent without extra environment toggles. Set `ENABLE_HOSTED_AGENTS=false` before provisioning only if you want a local-invocations project without the hosted-agent capability host and container registry.

To try a different model, set the `AZURE_FOUNDRY_MODEL_*` azd environment values before provisioning, or set fully custom deployment JSON via `AI_PROJECT_DEPLOYMENTS`. When `AI_PROJECT_DEPLOYMENTS` is not set, the defaults are:

```bash
azd env set AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME gpt-5.4
azd env set AZURE_FOUNDRY_MODEL_NAME gpt-5.4
azd env set AZURE_FOUNDRY_MODEL_VERSION 2026-03-05
azd env set AZURE_FOUNDRY_MODEL_SKU_NAME DataZoneStandard
azd env set AZURE_FOUNDRY_MODEL_SKU_CAPACITY 100
azd env set AZURE_FOUNDRY_AUX_MODEL_DEPLOYMENT_NAME gpt-5.4-mini
azd env set AZURE_FOUNDRY_AUX_MODEL_NAME gpt-5.4-mini
azd env set AZURE_FOUNDRY_AUX_MODEL_VERSION 2026-03-17
azd env set AZURE_FOUNDRY_AUX_MODEL_SKU_NAME DataZoneStandard
azd env set AZURE_FOUNDRY_AUX_MODEL_SKU_CAPACITY 50
azd env set AZURE_FOUNDRY_MODEL_API_MODE chat_completions
azd env set AZURE_FOUNDRY_AUTH_MODE entra_id
```

Set `AZURE_FOUNDRY_AUX_MODEL_DEPLOYMENT_NAME` to an empty value to provision only the primary model. For fully custom deployment JSON, set `AI_PROJECT_DEPLOYMENTS`.

## Local TUI passthrough

The local end-to-end path keeps the Hermes React/Ink TUI unchanged and swaps only the Python TUI backend:

```text
Hermes TUI
  -> tui_gateway.entry
  -> Foundry proxy backend
  -> local azd Invocations agent
  -> hosted Hermes gateway child process
```

Prepare the local dependencies once:

```bash
az login
cd third_party/hermes
uv sync --extra azure-identity   # azure-identity is an optional extra in hermes-agent; required for Foundry auth
cd ui-tui
npm install
cd ../../..
```

Then run it with two terminals. First start the local hosted agent:

```bash
azd ai agent run
```

Then launch the TUI in Foundry mode:

```bash
./scripts/run-foundry-tui.sh
```

The helper defaults to `http://127.0.0.1:8088`, `hermes-foundry-agent`, and the local azd path `HERMES_FOUNDRY_INVOCATIONS_PATH=/invocations` for localhost endpoints. It prefers `third_party/hermes/.venv/bin/python` so the TUI gateway uses the same synced Hermes dependencies as the rest of the dev loop. Override with `HERMES_PYTHON`, `HERMES_FOUNDRY_ENDPOINT`, `HERMES_FOUNDRY_INVOCATIONS_PATH`, `HERMES_FOUNDRY_AGENT_NAME`, or `HERMES_FOUNDRY_WORKSPACE_KEY` if needed. For the deployed hosted agent, use `./scripts/run-foundry-tui-remote.sh`; it reads the canonical `AZURE_AI_PROJECT_ENDPOINT`, agent name, and API version from the active azd environment, then clears the localhost-only invocation overrides. Remote invocations send the hosted-agents preview feature header required by the Foundry backend.

The Foundry proxy tunnels Hermes JSON-RPC calls as `kind: "hermes.rpc"` invoke payloads. The hosted agent starts a long-lived `tui_gateway.entry` child process, writes those JSON-RPC requests to the child over stdin, reads Hermes JSON-RPC frames from stdout, and streams prompt events back over SSE. Set `HERMES_GATEWAY_SRC_ROOT`, `HERMES_GATEWAY_PYTHON`, or `HERMES_GATEWAY_CWD` before `azd ai agent run` if the agent process cannot auto-detect the local Hermes checkout, Python virtualenv, or working directory.

### Auth and per-user sessions

`az login` is required everywhere — including against the localhost dev host. The proxy uses `DefaultAzureCredential` to acquire a bearer for the `https://ai.azure.com/.default` scope, decodes its own token to read the Entra `oid` claim, and uses `tui-{sha256(oid)[:16]}` as the per-user `agent_session_id`. Remote Foundry calls keep Entra authentication and also send Foundry header-isolation keys: `x-ms-user-isolation-key` is derived as `tui-user-{sha256(oid)[:16]}`, and `x-ms-chat-isolation-key` is derived from the active Foundry workspace/session key as `tui-chat-{sha256(agent_session_id)[:16]}`. These hashes are stable per user/session, so:

- The same user always reconnects to the same Foundry session, regardless of cwd or machine.
- Different users always land in distinct, isolated Foundry sessions and sandboxes.
- The raw `oid` is never sent on the wire or written to logs — only the prefixed hash is.

If `DefaultAzureCredential` cannot produce a token (no `az login`, no service principal), the proxy fails loudly with a "run az login" error before any RPC is attempted.

`HERMES_FOUNDRY_WORKSPACE_KEY` remains as an explicit override for tests, CI, or deliberate impersonation. `HERMES_FOUNDRY_BEARER_TOKEN` similarly overrides the credential acquisition with a pre-acquired token (useful for tests or automation that already holds a bearer). `HERMES_FOUNDRY_USER_ISOLATION_KEY` and `HERMES_FOUNDRY_CHAT_ISOLATION_KEY` override the derived header-isolation values when automation cannot derive them from Entra; override values may contain only alphanumeric characters, hyphens, and underscores.

### Persistent disk per session

The new foundry hosted-agent runtime gives every distinct `agent_session_id` its own sandbox filesystem that lives for the life of the session. In hosted Foundry, Hermes home defaults to `$HOME/.hermes` and the child cwd defaults to `$HOME/workspace`; the current runtime sets `HOME=/home/session`, so Hermes state and agent-created workspace files land on the session-mounted filesystem instead of the small image root filesystem. Local dev (without `FOUNDRY_HOSTING_ENVIRONMENT`) still defaults Hermes home to `~/.cache/hermes-foundry-tui/hermes-home` and cwd to the repo root so it doesn't trample a developer's real `~/.hermes`.

### Foundry child config

Before packaging or deploying the hosted agent, an azd hook renders an isolated Hermes config from azd outputs and the Dockerfile bakes it into the image. On startup, the hosted agent copies that generated config into `$HERMES_HOME/config.yaml`:

```yaml
model:
  provider: azure-foundry
  default: <AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME>
  base_url: <AZURE_FOUNDRY_BASE_URL>/openai/v1
  api_mode: <AZURE_FOUNDRY_MODEL_API_MODE>
  auth_mode: entra_id
providers:
  azure-foundry:
    stale_timeout_seconds: 300
```

By default, the rendered config does not include Foundry Toolbox MCP servers.
This avoids baking a broken MCP endpoint into the first deployment before a
toolbox exists. To enable one, create the toolbox, set the full MCP URL, and
redeploy so azd publishes a new hosted-agent version:

```bash
azd env set HERMES_FOUNDRY_TOOLBOX_MCP_URL '<full Foundry Toolbox MCP URL>'
azd deploy
```

When `HERMES_FOUNDRY_TOOLBOX_MCP_URL` is set, the renderer adds an `ftb`
MCP server using Streamable HTTP with native Microsoft Entra ID bearer auth
(`auth: entra_id`). Hermes mints a fresh token per request from the hosted
agent's managed identity via `DefaultAzureCredential`, scoped to
`https://ai.azure.com/.default`.

> Note: the `auth: entra_id` MCP transport support is currently carried as a
> cherry-pick on the vendored Hermes submodule
> (`feat/azure-mcp-bearer-auth`). Once that change lands upstream, the
> cherry-pick can be dropped.

The hosted agent does not rewrite this file on boot. In the current Foundry hosted-agent public preview, each session starts from the deployment image and then preserves the sandbox filesystem, so Hermes config changes made inside a session persist instead of being clobbered on restart. This also avoids inheriting your personal `~/.hermes` model settings during the Foundry path.

### Wake-on-maintenance invokes

The hosted agent also accepts bounded maintenance invokes so an external scheduler can wake a per-user Foundry session, run one maintenance pass, and then let the sandbox idle-suspend naturally:

```json
{
  "kind": "hermes.maintenance",
  "session_id": "tui-optional-session-id",
  "jobs": ["default"],
  "timeout_seconds": 540
}
```

`default` runs one cron tick, image/document cache cleanup, expired debug-paste sweep, and session pruning when `sessions.auto_prune` is enabled in Hermes config. Use `jobs: ["all"]` or include `"curator"` explicitly for the skill curator; it runs synchronously and is still gated by curator config unless `force_curator` is true. The Foundry adapter wakes the Hermes gateway and calls its `maintenance.run` RPC, where each pass is protected by a durable `$HERMES_HOME/foundry-maintenance/maintenance.lock`; the adapter records JSONL history at `$HERMES_HOME/foundry-maintenance/history.jsonl` and emits a `maintenance.summary` event to `session_id` when a TUI event buffer is active. Kanban dispatch is intentionally not part of the one-shot maintenance path because it spawns background workers by design.

The local proxy keeps only the routing state needed to connect the TUI to the hosted Hermes child. RPC methods it does not need to orchestrate locally are forwarded to the child gateway, including commands, config, session history, completion, shell/tool/plugin/model/voice/rollback/browser/skills surfaces, and future gateway methods.

To exercise interrupt locally, restart `azd ai agent run` after code changes, send a longer TUI prompt, and press `Ctrl+C` while the real Hermes child is running. Clarify/approval-style controls flow through when the real gateway emits those requests.

## Cloud deployment

```bash
azd auth login
azd up
```

`azd up` provisions the Microsoft Foundry project resources, including the default model deployment, renders the hosted Hermes config, builds the hosted-agent container, and publishes the agent through the `azure.ai.agents` extension. Re-run `azd up` or `azd deploy` after changing model-related azd environment values or Foundry Toolbox settings so the image contains the updated config.

The hosted agent runs under its own managed identity. A post-deploy hook grants that identity the `Cognitive Services OpenAI User` role on the AI Services account so Hermes can call the deployed model with `DefaultAzureCredential`.

After deployment, launch the local TUI against the remote hosted agent:

```bash
az login
./scripts/run-foundry-tui-remote.sh
```

Pass an optional session name as the first argument to target a specific named Foundry session instead of the per-user derived `tui-<sha256(oid)>` key. This sets `HERMES_FOUNDRY_WORKSPACE_KEY` for you and is handy for spinning up a fresh session/sandbox (e.g. after a previous session is stuck deleting):

```bash
./scripts/run-foundry-tui-remote.sh barry
```

The deployed container is built from the repository root so it includes `third_party/hermes`; `agent/Dockerfile` installs the hosted-agent shim plus the pinned Hermes submodule into the image. Keep the submodule initialized before deploying:

```bash
./scripts/init-hermes.sh
azd up
```

## Current status

The agent accepts the `hermes.rpc` tunnel used by the TUI path; direct text Invocations payloads are rejected. Per-user session isolation lands the workspace key on the Entra `oid` (hashed), Hermes home and the runtime cwd on the Foundry session-scoped sandbox filesystem, and the deployed image now includes the pinned Hermes runtime. Durable event logs and reconnect cursors are later hardening steps.

## What runs where

The hosted Foundry sandbox is your **persistent remote workspace** — full filesystem and shell, scoped to your Entra identity. The local TUI is just a renderer + a thin proxy. That changes what "the shell" means compared to local Hermes:

| Surface | Where it runs | Notes |
|---|---|---|
| `shell.exec`, agent terminal tools, file edits, `/cd` | **Foundry sandbox** | tools start in persistent `$HOME/workspace` (currently `/home/session/workspace`) — `ls` shows the sandbox, not your laptop |
| Path completion (`complete.path`) | Foundry sandbox | matches paths the agent will actually use |
| Slash commands, `/help`, `/skills`, `/cron`, `/model` | Foundry sandbox | full Hermes catalog |
| Hermes session state (history, memory, skills) | Foundry sandbox `$HOME/.hermes` (currently `/home/session/.hermes`) | persistent across reconnects |
| Clipboard image paste (`Ctrl+V`) | Local read at the proxy, **bytes uploaded** to the sandbox `$HOME/.hermes/images/` | works the natural way |
| Drag-drop a local file (`input.detect_drop`) | Local detection at the proxy | image files have their bytes uploaded to the sandbox; non-image files generate a `[User attached file: …]` marker that goes into the prompt text |
| `image.attach <path>` | Proxy resolves the path locally first; if it exists on your laptop, bytes are uploaded. Otherwise the path is treated as sandbox-relative | covers both local-laptop attachments and references to files already in the sandbox |
| Voice (`voice.{toggle,record,tts}`) | Not yet supported in foundry mode | local mic/speaker plumbing isn't wired up; voice RPCs currently surface upstream errors |
| TUI rendering, Ink keybindings, composer | Local | every TUI redraw and keystroke is local |

The wire change that enables clipboard / image bytes upload is an optional `bytes_b64` + `filename` on the hosted `image.attach` RPC. Local Hermes ignores it; foundry mode uses it.
