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
azd ai agent run
```

In another terminal:

```bash
azd ai agent invoke --local --protocol invocations "hello"
```

The local agent starts on port `8088` by default. The current implementation is only an Invocations-shaped stub; the Hermes hosted runtime gets wired in the next phase.

## Cloud deployment

```bash
azd auth login
azd up
```

`azd up` provisions the Microsoft Foundry project resources, builds the hosted-agent container, and publishes the agent through the `azure.ai.agents` extension.

## Current status

The agent is a small Invocations protocol stub, not the real Hermes worker yet. The next implementation step is to add the TUI gateway backend seam in Hermes and replace the stub invocation handler with the hosted Hermes runtime.
