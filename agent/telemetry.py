"""Application Insights telemetry for the Foundry invocation adapter.

The hosted ``InvocationAgentServerHost`` already configures Azure Monitor
log + trace export automatically: ``AgentServerHost.__init__`` calls
``configure_observability(connection_string=...)`` by default, reading
``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment, setting the
root logger to INFO, and exporting logs via the bundled
``microsoft-opentelemetry`` distro. We therefore do NOT call
``configure_azure_monitor`` ourselves (that would double-configure) and we do
NOT add an explicit ``azure-monitor-opentelemetry`` dependency.

This module's job is small:

1. ``ensure_connection_string_env`` — make the connection string available to
   the host before it is constructed. The hosted runtime auto-injects the
   reserved ``APPLICATIONINSIGHTS_CONNECTION_STRING``, but it points at a
   platform-managed Application Insights (outside our subscription, not
   queryable by us). ``agent.yaml`` passes the connection string for *our*
   provisioned resource under the non-reserved
   ``APPLICATION_INSIGHTS_CONNECTION_STRING`` alias; when that alias is present
   we copy it onto the canonical name, overriding the platform value, so
   telemetry flows to the resource we own and can query.
2. ``record_maintenance`` — emit one structured log record per maintenance run
   (and per job) so per-job outcomes are observable. A run that returns HTTP
   200 with a failed job is invisible to the routine platform (which only flags
   non-2xx), so these records are what make failures visible.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_CANONICAL_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
_ALIAS_ENV = "APPLICATION_INSIGHTS_CONNECTION_STRING"

logger = logging.getLogger("hermes.maintenance")


def _is_usable(value: str) -> bool:
    """A connection string we can actually use (substituted and non-empty)."""

    return bool(value) and "${" not in value


def _redact(value: str) -> str:
    """Show enough of a GUID-like secret to identify it without leaking it."""

    return f"{value[:8]}…" if len(value) > 8 else "set"


def _describe(conn_str: str) -> str:
    """Build a redacted one-line summary of a connection string for logging."""

    parts = dict(
        kv.split("=", 1) for kv in conn_str.split(";") if "=" in kv
    )
    ikey = parts.get("InstrumentationKey", "")
    app_id = parts.get("ApplicationId", "")
    endpoint = parts.get("IngestionEndpoint", "")
    return (
        f"ikey={_redact(ikey) if ikey else 'none'} "
        f"appId={_redact(app_id) if app_id else 'none'} "
        f"ingestion={endpoint or 'none'}"
    )


def ensure_connection_string_env() -> None:
    """Populate the canonical App Insights env var before host construction.

    Prefers our explicit ``APPLICATION_INSIGHTS_CONNECTION_STRING`` alias (the
    resource we provisioned and can query) over any platform-injected canonical
    value, because explicit customer configuration must win. Falls back to the
    platform value only when our alias is missing/unsubstituted. Must be called
    before ``InvocationAgentServerHost()`` so the host's automatic observability
    setup picks up the connection string. Emits a redacted summary to stdout so
    the chosen telemetry destination is visible in container logs (this print
    is intentionally independent of Azure Monitor, which may itself be the thing
    that is misconfigured).
    """

    alias = (os.environ.get(_ALIAS_ENV) or "").strip()
    canonical = (os.environ.get(_CANONICAL_ENV) or "").strip()

    if _is_usable(alias):
        os.environ[_CANONICAL_ENV] = alias
        source = "alias-override-platform" if canonical and canonical != alias else "alias"
        chosen = alias
    elif canonical:
        source = "platform"
        chosen = canonical
    else:
        source = "none"
        chosen = ""

    summary = _describe(chosen) if chosen else "no connection string available"
    print(
        f"[hermes.telemetry] App Insights destination source={source} {summary}",
        flush=True,
    )


def record_maintenance(result: dict[str, Any]) -> None:
    """Emit the maintenance run outcome and per-job statuses.

    Records propagate to the root logger, which the host exports to Application
    Insights. Safe to call regardless of whether telemetry export is active.
    Custom-dimension keys avoid reserved ``logging.LogRecord`` attribute names.
    """

    try:
        status = str(result.get("status") or "unknown")
        jobs = result.get("jobs")
        jobs = jobs if isinstance(jobs, list) else []
        run_id = result.get("run_id")

        run_level = logging.ERROR if status == "error" else logging.INFO
        logger.log(
            run_level,
            "hermes maintenance run %s status=%s",
            run_id,
            status,
            extra={
                "run_id": run_id,
                "maintenance_status": status,
                "job_count": len(jobs),
                "duration_seconds": result.get("duration_seconds"),
                "maintenance_reason": result.get("reason"),
            },
        )

        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_status = str(job.get("status") or "unknown")
            job_level = logging.ERROR if job_status == "error" else logging.INFO
            logger.log(
                job_level,
                "hermes maintenance job %s status=%s",
                job.get("name"),
                job_status,
                extra={
                    "run_id": run_id,
                    "job_name": job.get("name"),
                    "job_status": job_status,
                    "duration_seconds": job.get("duration_seconds"),
                    "job_error": job.get("error"),
                    "job_reason": job.get("reason"),
                },
            )
    except Exception:  # pragma: no cover - telemetry must never break a run
        pass
