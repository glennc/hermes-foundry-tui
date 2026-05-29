"""Self-provisioning of the daily Hermes maintenance routine.

A Foundry *routine* runs maintenance on a schedule, but it has to target the
specific Foundry *session* a user actually started — the routine's
``action.session_id`` becomes the ``agent_session_id`` it invokes the agent
with, so maintenance runs inside that user's terminal sandbox. Creating the
routine out-of-band (by hand, or in an azd hook) is brittle: the session key is
per-user and a fresh sandbox is minted whenever a new agent version is deployed.

Instead, the agent provisions the routine itself. On the first terminal
(``hermes.rpc``) invocation for a given session, ``schedule_maintenance_routine``
fires a best-effort background task that ensures a routine
``hermes-maint-<hash(session_id)>`` exists and matches the desired spec, calling
back the project's routines REST API with the hosted managed identity.

Design constraints:

* **Never break the user's RPC.** All work runs in a fire-and-forget background
  task; every failure is swallowed (and logged), never raised to the handler.
* **Idempotent, at most once per session per process.** A successful provision
  is cached; concurrent first calls for the same session are de-duplicated via
  an in-flight guard.
* **Validate-and-repair.** An existing routine is inspected; if it drifts from
  the desired spec (disabled, wrong session/agent/input/schedule) it is repaired
  with a ``PUT`` rather than trusted blindly.
* **Self-contained.** Uses stdlib ``urllib`` (no new dependency) with explicit
  timeouts, and a module-level cached ``DefaultAzureCredential``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("hermes.maintenance")

_DEFAULT_AGENT_NAME = "hermes-foundry-agent"
_DEFAULT_CRON = "0 9 * * *"
_DEFAULT_TIMEZONE = "UTC"
_DEFAULT_ROUTINE_PREFIX = "hermes-maint-"
_DEFAULT_API_VERSION = "2025-05-15-preview"
_TOKEN_SCOPE = "https://ai.azure.com/.default"
_HTTP_TIMEOUT_S = 15.0
# Cooldown before retrying after a failure, so a busy session cannot hammer the
# routines API. RBAC denials get a long cooldown (an operator must grant the MI
# permissions); transient failures get a short one.
_DENIED_COOLDOWN_S = 30 * 60.0
_RETRY_COOLDOWN_S = 5 * 60.0

# Per-session provisioning state, guarded by ``_state_lock``.
_provisioned: set[str] = set()
_in_flight: set[str] = set()
_cooldown_until: dict[str, float] = {}
_state_lock = threading.Lock()

# Strongly-referenced background tasks so the event loop does not GC them
# mid-flight (asyncio only holds a weak reference to scheduled tasks).
_background_tasks: set[asyncio.Task[Any]] = set()

_credential: Any = None
_credential_lock = threading.Lock()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _autoprovision_disabled() -> bool:
    return _truthy(os.environ.get("HERMES_FOUNDRY_DISABLE_ROUTINE_AUTOPROVISION"))


def _project_endpoint() -> str:
    raw = (
        os.environ.get("HERMES_FOUNDRY_PROJECT_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or ""
    ).strip()
    return raw.rstrip("/")


def _agent_name() -> str:
    return (os.environ.get("HERMES_FOUNDRY_AGENT_NAME") or _DEFAULT_AGENT_NAME).strip()


def _cron() -> str:
    return (os.environ.get("HERMES_FOUNDRY_MAINTENANCE_CRON") or _DEFAULT_CRON).strip()


def _timezone() -> str:
    return (
        os.environ.get("HERMES_FOUNDRY_MAINTENANCE_TIMEZONE") or _DEFAULT_TIMEZONE
    ).strip()


def _api_version() -> str:
    return (
        os.environ.get("HERMES_FOUNDRY_ROUTINE_API_VERSION") or _DEFAULT_API_VERSION
    ).strip()


def _routine_name(session_id: str) -> str:
    """Deterministic, charset-safe routine name for a session.

    Hashing guarantees uniqueness and a valid ``[a-z0-9-]`` name regardless of
    what the session key contains (it may be an arbitrary
    ``HERMES_FOUNDRY_WORKSPACE_KEY`` rather than a ``tui-`` hash).
    """

    prefix = (
        os.environ.get("HERMES_FOUNDRY_MAINTENANCE_ROUTINE_PREFIX")
        or _DEFAULT_ROUTINE_PREFIX
    ).strip()
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}"


def _desired_routine(session_id: str) -> dict[str, Any]:
    return {
        "description": (
            "Auto-provisioned daily Hermes maintenance for terminal session "
            f"{session_id}."
        ),
        "enabled": True,
        "triggers": {
            "daily": {
                "type": "schedule",
                "cron_expression": _cron(),
                "time_zone": _timezone(),
            }
        },
        "action": {
            "type": "invoke_agent_invocations_api",
            "agent_name": _agent_name(),
            "session_id": session_id,
            "input": {"kind": "hermes.maintenance", "jobs": ["all"]},
        },
    }


def _routine_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True when the existing routine already satisfies the desired spec."""

    if not isinstance(existing, dict):
        return False
    if existing.get("enabled") is not True:
        return False

    action = existing.get("action")
    if not isinstance(action, dict):
        return False
    desired_action = desired["action"]
    if action.get("type") != desired_action["type"]:
        return False
    if action.get("session_id") != desired_action["session_id"]:
        return False
    if action.get("agent_name") != desired_action["agent_name"]:
        return False
    if action.get("input") != desired_action["input"]:
        return False

    triggers = existing.get("triggers")
    if not isinstance(triggers, dict):
        return False
    desired_trigger = desired["triggers"]["daily"]
    schedule_triggers = [
        trigger
        for trigger in triggers.values()
        if isinstance(trigger, dict) and trigger.get("type") == "schedule"
    ]
    # Exactly one schedule trigger, matching the desired cadence: more than one
    # would make maintenance run more often than intended (treated as drift).
    if len(schedule_triggers) != 1:
        return False
    trigger = schedule_triggers[0]
    return (
        trigger.get("cron_expression") == desired_trigger["cron_expression"]
        and trigger.get("time_zone") == desired_trigger["time_zone"]
    )


def _get_credential() -> Any:
    global _credential
    if _credential is None:
        with _credential_lock:
            if _credential is None:
                from azure.identity import DefaultAzureCredential

                _credential = DefaultAzureCredential()
    return _credential


def _bearer_token() -> str:
    return _get_credential().get_token(_TOKEN_SCOPE).token


def _request(method: str, url: str, token: str, body: dict[str, Any] | None) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url=url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def ensure_maintenance_routine(session_id: str) -> None:
    """Ensure a daily maintenance routine exists for ``session_id``.

    Best-effort and idempotent: caches success per process, de-duplicates
    concurrent first calls, validates/repairs an existing routine, and never
    raises. Intended to run off the request path (see
    ``schedule_maintenance_routine``).
    """

    session_id = (session_id or "").strip()
    if not session_id or _autoprovision_disabled():
        return

    with _state_lock:
        if session_id in _provisioned or session_id in _in_flight:
            return
        deadline = _cooldown_until.get(session_id)
        if deadline is not None and time.monotonic() < deadline:
            return
        _in_flight.add(session_id)

    try:
        _provision(session_id)
    except Exception:  # pragma: no cover - provisioning must never break a run
        _set_cooldown(session_id, _RETRY_COOLDOWN_S)
        logger.warning(
            "hermes maintenance routine provisioning failed for session %s",
            session_id,
            exc_info=True,
        )
    finally:
        with _state_lock:
            _in_flight.discard(session_id)


def _mark_provisioned(session_id: str) -> None:
    with _state_lock:
        _provisioned.add(session_id)
        _cooldown_until.pop(session_id, None)


def _set_cooldown(session_id: str, seconds: float) -> None:
    with _state_lock:
        _cooldown_until[session_id] = time.monotonic() + seconds


def _provision(session_id: str) -> None:
    endpoint = _project_endpoint()
    if not endpoint:
        _set_cooldown(session_id, _RETRY_COOLDOWN_S)
        logger.warning(
            "cannot provision maintenance routine: FOUNDRY_PROJECT_ENDPOINT is unset"
        )
        return

    name = _routine_name(session_id)
    desired = _desired_routine(session_id)
    api_version = _api_version()
    base = f"{endpoint}/routines/{name}?api-version={api_version}"
    token = _bearer_token()

    status, body = _request("GET", base, token, None)
    if status == 200:
        try:
            existing = json.loads(body)
        except json.JSONDecodeError:
            existing = {}
        if _routine_matches(existing, desired):
            _mark_provisioned(session_id)
            logger.info(
                "hermes maintenance routine %s already current for session %s",
                name,
                session_id,
            )
            return
        logger.info(
            "hermes maintenance routine %s drifted; repairing for session %s",
            name,
            session_id,
        )
    elif status == 404:
        logger.info(
            "hermes maintenance routine %s absent; creating for session %s",
            name,
            session_id,
        )
    elif status in (401, 403):
        _set_cooldown(session_id, _DENIED_COOLDOWN_S)
        logger.error(
            "hermes maintenance routine provisioning denied (HTTP %s) for session "
            "%s; the agent's managed identity likely lacks routine permissions. "
            "Response: %s",
            status,
            session_id,
            body[:500],
        )
        return
    else:
        _set_cooldown(session_id, _RETRY_COOLDOWN_S)
        logger.warning(
            "unexpected HTTP %s reading maintenance routine %s for session %s: %s",
            status,
            name,
            session_id,
            body[:500],
        )
        return

    put_status, put_body = _request("PUT", base, token, desired)
    if 200 <= put_status < 300:
        _mark_provisioned(session_id)
        logger.info(
            "hermes maintenance routine %s provisioned for session %s",
            name,
            session_id,
        )
    elif put_status in (401, 403):
        _set_cooldown(session_id, _DENIED_COOLDOWN_S)
        logger.error(
            "hermes maintenance routine PUT denied (HTTP %s) for session %s; the "
            "agent's managed identity likely lacks routine permissions. Response: %s",
            put_status,
            session_id,
            put_body[:500],
        )
    else:
        _set_cooldown(session_id, _RETRY_COOLDOWN_S)
        logger.warning(
            "failed to PUT maintenance routine %s (HTTP %s) for session %s: %s",
            name,
            put_status,
            session_id,
            put_body[:500],
        )


def _on_task_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning(
            "background maintenance routine provisioning task errored",
            exc_info=exc,
        )


def schedule_maintenance_routine(session_id: str) -> None:
    """Fire-and-forget provisioning of the maintenance routine for a session.

    Safe to call from the request handler: returns immediately, adds no latency,
    and never raises. No-op when auto-provisioning is disabled, the session id is
    empty, or there is no running event loop.
    """

    session_id = (session_id or "").strip()
    if not session_id or _autoprovision_disabled():
        return
    with _state_lock:
        if session_id in _provisioned or session_id in _in_flight:
            return
        deadline = _cooldown_until.get(session_id)
        if deadline is not None and time.monotonic() < deadline:
            return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = loop.create_task(asyncio.to_thread(ensure_maintenance_routine, session_id))
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
