Hermes Foundry TUI Project Brief
================================

Goal
----
Build a focused integration that lets the existing Hermes TUI talk to a Hermes
agent hosted on Azure AI Foundry.

The desired user experience is that a user can use the Hermes TUI as close to as-is but against a remote Hermes agent running in Microsoft Foundry. It should aith with Entra/DefaultAzureCredential. Each TUI user should get their own foundry session that stays persistent for that user.

The TUI should keep its current React/Ink interaction model while the backend
routes turns, cancellations, steering, approvals, and session state through the
Foundry-hosted Hermes worker.

Core Idea
---------
Add a small backend seam to the Hermes TUI gateway. Today the TUI starts
`python -m tui_gateway.entry` and speaks newline-delimited JSON-RPC over stdio.
The new mode should preserve that TUI protocol and swap the Python backend:

  local mode:
    TUI -> tui_gateway.entry -> local AIAgent

  foundry mode:
    TUI -> tui_gateway.entry -> FoundryProxyBackend -> Foundry Invocations -> hosted Hermes

This should not be a Hermes messaging gateway plugin. It is a TUI gateway
backend adapter that implements the TUI JSON-RPC contract and translates it to
the hosted worker's Invocations contract.

Protocol Direction
------------------
Use Foundry Invocations from the proxy to the hosted agent.

Invocations is the right abstraction because Hermes is a hosted worker with
custom session routing, tools, approvals, cancellation, steering, filesystem
state, and delivery/control semantics. Responses is model-shaped; this is
agent-runtime-shaped.

Expected Foundry call examples:

  POST /agents/{agent}/endpoint/protocols/invocations?agent_session_id={workspace}&api-version=v1
  GET  /agents/{agent}/endpoint/protocols/invocations/{invocation_id}?agent_session_id={workspace}&api-version=v1
  POST /agents/{agent}/endpoint/protocols/invocations/{invocation_id}/cancel?agent_session_id={workspace}&api-version=v1
  POST /agents/{agent}/endpoint/protocols/invocations?agent_session_id={workspace}&api-version=v1
       with kind=hermes.control for approvals, steer, model controls, prompt responses, and session controls

Auth
----
Use Entra ID as the default authentication model.

The local proxy obtains a short-lived bearer token for:

  https://ai.azure.com/.default

using DefaultAzureCredential, which should work with Azure CLI login for local
developer use. The signed-in user or group needs project-scoped Azure AI User
permission on the Foundry project. Persist endpoint, agent name, and workspace
configuration; do not persist bearer tokens.

`az login` is required everywhere — including against the localhost dev stub —
because the proxy derives the per-user workspace key from the Entra `oid`
claim of the bearer it just acquired. If `DefaultAzureCredential` cannot
produce a token, the proxy fails loudly with a "run az login" message before
any RPC is attempted.

Configuration to capture:

  AZURE_AI_PROJECT_ENDPOINT or HERMES_FOUNDRY_PROJECT_ENDPOINT
  HERMES_FOUNDRY_AGENT_NAME
  HERMES_FOUNDRY_WORKSPACE_KEY  (explicit override only — for tests, CI,
                                 or deliberate impersonation. Normally the
                                 workspace key is derived from the Entra
                                 oid, hashed.)

Workspace identity
------------------
One TUI user → one Foundry session → one persistent sandbox.

The proxy decodes its own bearer token (no signature verify), reads the
`oid` claim (Entra Object ID, falling back to `sub`), and uses
`tui-{sha256(oid)[:16]}` as the `agent_session_id` for every Foundry
Invocations call. The hash is stable per user, so the same user always
reconnects to the same sandbox regardless of cwd or machine; different
users always land in distinct sandboxes. The raw `oid` is never sent on
the wire or written to logs.

The hosted agent persists Hermes home under `$HOME/.hermes` so all Hermes
state (config, sessions, memory, workspace files) lives on Foundry's
session-scoped persistent disk.

Initial JSON-RPC Scope
----------------------
Implement the smallest useful TUI backend first:

  gateway.ready
  commands.catalog
  config.get for keys the TUI needs at startup
  session.create
  session.status
  session.close
  prompt.submit
  session.interrupt
  session.steer
  approval.respond

For the first milestone, session list/resume/title, slash command parity, image
attachments, clipboard integration, branch/compress/undo/retry, local shell
commands, and spawn-tree persistence can be deferred or surfaced as unsupported
with clear TUI-visible messages.

TUI Event Stream
----------------
Full TUI fidelity requires the hosted worker to emit TUI-shaped events.

One possible implementation wwe need to validate is an append-only event log on each async
invocation record, not transport-level streaming. The worker assigns
monotonically increasing event sequence numbers; the FoundryProxyBackend polls
with a cursor and re-emits events to the existing TUI as JSON-RPC events.

Events to support:

  message.start
  message.delta
  message.complete
  thinking.delta
  reasoning.delta
  reasoning.available
  status.update
  tool.start
  tool.progress
  tool.complete
  approval.request
  clarify.request
  sudo.request
  secret.request
  error

Blocking prompts need a control path. Approvals already map naturally to
hermes.control. Clarify, sudo, and secret prompts should use the same pattern:
the worker appends a prompt event and waits; the proxy sends a hermes.control
response with the request_id; the worker unblocks and continues the same turn.

Project Boundaries
------------------
In scope:

  - TUI gateway backend seam.
  - Local FoundryProxyBackend.
  - Foundry Invocations client and auth.
  - TUI contract for hosted Hermes.
  - Minimal remote session and control semantics needed by the TUI.

Out of scope:

  - Replacing the React/Ink UI.
  - Treating hosted Hermes as a plain OpenAI-compatible model endpoint.

Open Questions
--------------
  - Should the backend seam be environment-only, e.g. HERMES_TUI_BACKEND=foundry,
    or exposed as a first-class CLI flag, e.g. hermes --tui --foundry?
  - Which TUI slash commands should execute locally versus remotely?
  - How much local filesystem affordance should remain when the actual tools run
    in the hosted Foundry sandbox?
  - Should the event log be bounded or retained with the invocation record for
    debugging and reconnect?

Resolved
--------
  - Hosted TUI sessions are scoped per user. Workspace key is derived from
    the Entra `oid` claim (hashed) and is stable across cwds and machines.
    See "Workspace identity" above.
