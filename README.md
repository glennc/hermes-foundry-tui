# Hermes Foundry TUI

PoC integration repo for running the existing Hermes React/Ink TUI against a Hermes agent hosted through Azure AI Foundry.

This repo intentionally stays separate from Hermes. Hermes source is pinned as a Git submodule under `third_party/hermes`, while this repo owns azd infrastructure, deployment glue, local configuration, and integration scaffolding.

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

## Initial setup

```bash
git clone --recurse-submodules <repo-url>
cd hermes-foundry-tui
./scripts/init-hermes.sh
```

If the repo was cloned without submodules, `./scripts/init-hermes.sh` will fetch `third_party/hermes`.

Hermes changes for this PoC live on the `foundry-tui-poc` branch of `https://github.com/glennc/hermes-agent.git`. Work inside `third_party/hermes`, commit and push that branch, then update the submodule pointer in this repo.

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
azd ai agent invoke --local --protocol invocations "hello"
```

The local agent starts on port `8088` by default. Direct text invokes still hit a small streaming stub, while the TUI path uses the `hermes.rpc` tunnel described below.

`azd provision` creates the Foundry project and a default OpenAI-family model deployment for the dev loop. The default deployment is `gpt-4.1-mini` with `auth_mode=default_azure_credential`, so local runs use your Azure developer identity and hosted runs use the agent identity instead of API keys.

Override the model with azd environment values before provisioning:

```bash
azd env set AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME gpt-4.1-mini
azd env set AZURE_FOUNDRY_MODEL_NAME gpt-4.1-mini
azd env set AZURE_FOUNDRY_MODEL_VERSION 2025-04-14
azd env set AZURE_FOUNDRY_MODEL_SKU_NAME GlobalStandard
azd env set AZURE_FOUNDRY_MODEL_SKU_CAPACITY 1
azd env set AZURE_FOUNDRY_MODEL_API_MODE chat_completions
azd env set AZURE_FOUNDRY_AUTH_MODE default_azure_credential
```

For fully custom deployment JSON, set `AI_PROJECT_DEPLOYMENTS`; when it is empty, the Bicep defaults above are used.

## Local TUI proof slice

The local end-to-end slice keeps the Hermes React/Ink TUI unchanged and swaps only the Python TUI backend:

```text
Hermes TUI
  -> tui_gateway.entry
  -> Foundry proxy backend
  -> local azd Invocations agent
  -> hosted Hermes gateway child process
```

Run it with two terminals. First start the local hosted agent:

```bash
azd ai agent run
```

Then launch the TUI in Foundry mode:

```bash
./scripts/run-foundry-tui.sh
```

The helper defaults to `http://127.0.0.1:8088`, `hermes-foundry-agent`, and the local azd path `HERMES_FOUNDRY_INVOCATIONS_PATH=/invocations` for localhost endpoints. Override with `HERMES_FOUNDRY_ENDPOINT`, `HERMES_FOUNDRY_INVOCATIONS_PATH`, `HERMES_FOUNDRY_AGENT_NAME`, or `HERMES_FOUNDRY_WORKSPACE_KEY` if needed. Cloud endpoints can omit `HERMES_FOUNDRY_INVOCATIONS_PATH` to use the default Foundry route.

The Foundry proxy now tunnels selected Hermes JSON-RPC calls as `kind: "hermes.rpc"` invoke payloads. The hosted agent starts a long-lived `tui_gateway.entry` child process, writes those JSON-RPC requests to the child over stdin, reads Hermes JSON-RPC frames from stdout, and streams prompt events back over SSE. Set `HERMES_GATEWAY_SRC_ROOT`, `HERMES_GATEWAY_PYTHON`, or `HERMES_GATEWAY_CWD` before `azd ai agent run` if the agent process cannot auto-detect the local Hermes checkout, Python virtualenv, or working directory.

Before launching the child gateway, the agent writes an isolated Hermes config from azd outputs:

```yaml
model:
  provider: azure-foundry
  default: <AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME>
  base_url: <AZURE_OPENAI_ENDPOINT>/openai/v1
  api_mode: <AZURE_FOUNDRY_MODEL_API_MODE>
  auth_mode: default_azure_credential
```

This avoids inheriting your personal `~/.hermes` model settings during the Foundry proof path.

Supported in this slice: TUI startup, command catalog, config hydration, real Hermes `session.create`, prompt submission through the Hermes child process, streamed `message.*` and `status.update` events, interrupt, clarify/sudo/secret/approval responses, title, usage, and clear/new session. Local-only capabilities such as shell commands, image attach, resume, branch, and compress still return explicit unsupported messages from the local proxy.

To exercise interrupt locally, restart `azd ai agent run` after code changes, send a longer TUI prompt, and press `Ctrl+C` while the real Hermes child is running. Clarify/approval-style controls now flow through when the real gateway emits those requests; the older direct text invoke stub still handles synthetic `clarify`/`slow` prompts outside the TUI RPC path.

## Cloud deployment

```bash
azd auth login
azd up
```

`azd up` provisions the Microsoft Foundry project resources, including the default model deployment, builds the hosted-agent container, and publishes the agent through the `azure.ai.agents` extension.

## Current status

The agent supports the original Invocations stub for direct text invokes and the new `hermes.rpc` tunnel used by the TUI proof path. The RPC tunnel is intentionally in-memory and local-first; durable event logs, reconnect cursors, and packaging the full Hermes runtime into the deployed container are later hardening steps.
