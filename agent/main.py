from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

import routine_provisioner
import telemetry

telemetry.ensure_connection_string_env()

app = InvocationAgentServerHost()
_GATEWAY_READY_TIMEOUT_S = 45.0
_RPC_RESPONSE_TIMEOUT_S = 60.0
_RPC_STREAM_IDLE_TIMEOUT_S = 15 * 60.0
_DEFAULT_EVENT_BUFFER_SIZE = 1000
_DEFAULT_MAINTENANCE_TIMEOUT_S = 9 * 60.0
_DEFAULT_MAINTENANCE_HISTORY_MAX_BYTES = 1024 * 1024
_BUFFER_SHUTDOWN = object()
_BUFFER_OVERFLOW = object()
_maintenance_process_lock = threading.Lock()


def _event_buffer_capacity() -> int:
    raw = (os.environ.get("HERMES_FOUNDRY_EVENT_BUFFER_SIZE") or "").strip()
    if not raw:
        return _DEFAULT_EVENT_BUFFER_SIZE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_EVENT_BUFFER_SIZE
    return value if value > 0 else _DEFAULT_EVENT_BUFFER_SIZE


class _EventBuffer:
    """Per-session ring buffer with monotonic seq and live subscribers.

    The buffer is the source of truth for every event emitted by the hosted
    Hermes gateway for a given session_id. Events are stamped with a seq
    before fan-out so subscribers can resume after a transport blip via
    `since_seq` cursors. Bounded retention drops the oldest event when
    `capacity` is exceeded; `last_dropped_seq` lets new subscribers detect
    a replay gap.
    """

    __slots__ = ("events", "next_seq", "last_dropped_seq", "subscribers")

    def __init__(self, capacity: int) -> None:
        self.events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=capacity)
        self.next_seq: int = 0
        self.last_dropped_seq: int = -1
        self.subscribers: list[asyncio.Queue[Any]] = []

    def append(self, frame: dict[str, Any]) -> int:
        seq = self.next_seq
        self.next_seq = seq + 1
        params = frame.get("params")
        if isinstance(params, dict):
            params["seq"] = seq
        maxlen = self.events.maxlen
        if maxlen is not None and len(self.events) == maxlen:
            dropped_seq, _ = self.events[0]
            if dropped_seq > self.last_dropped_seq:
                self.last_dropped_seq = dropped_seq
        self.events.append((seq, frame))
        stale: list[asyncio.Queue[Any]] = []
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                stale.append(q)
        for q in stale:
            self.close_subscription(q)
            self._signal_queue(q, _BUFFER_OVERFLOW)
        return seq

    def open_subscription(
        self, since_seq: int
    ) -> tuple[list[dict[str, Any]], int, asyncio.Queue[Any]]:
        replay = [frame for seq, frame in self.events if seq > since_seq]
        queue_maxsize = max(1, self.events.maxlen or 1)
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self.subscribers.append(queue)
        return replay, self.last_dropped_seq, queue

    def close_subscription(self, queue: asyncio.Queue[Any]) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass

    def shutdown(self) -> None:
        for q in self.subscribers:
            self._signal_queue(q, _BUFFER_SHUTDOWN)
        self.subscribers.clear()

    @staticmethod
    def _signal_queue(queue: asyncio.Queue[Any], item: object) -> None:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


def _jsonrpc_error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _sse_frame(value: dict[str, Any]) -> str:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _positive_float(value: Any, default: float, *, minimum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _is_foundry_hosted() -> bool:
    return bool(os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT", "").strip())


def _user_home_candidates() -> list[Path]:
    candidates: list[Path] = []
    home = (os.environ.get("HOME") or "").strip()
    if home:
        candidates.append(Path(home).expanduser())
    try:
        import pwd

        passwd_home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        passwd_home = ""
    if passwd_home:
        candidates.append(Path(passwd_home).expanduser())

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _default_child_hermes_home() -> Path:
    configured = _first_env("HERMES_CHILD_HOME", "HERMES_GATEWAY_HOME")
    if configured:
        return Path(configured).expanduser()

    if _is_foundry_hosted():
        for home in _user_home_candidates():
            hermes_home = home / ".hermes"
            if _ensure_writable_directory(hermes_home):
                return hermes_home
        checked = ", ".join(str(home / ".hermes") for home in _user_home_candidates())
        raise RuntimeError(f"No writable Foundry Hermes home found. Checked: {checked}")

    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache_root / "hermes-foundry-tui" / "hermes-home"


def _prepare_child_hermes_home() -> Path:
    hermes_home = _default_child_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home


def _resolve_hermes_root() -> Path:
    configured = (
        os.environ.get("HERMES_GATEWAY_SRC_ROOT")
        or os.environ.get("HERMES_PYTHON_SRC_ROOT")
        or ""
    ).strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    here = Path(__file__).resolve()
    candidates.extend(
        [
            Path.cwd() / "third_party" / "hermes",
            Path.cwd().parent / "third_party" / "hermes",
            here.parent / "third_party" / "hermes",
            here.parent.parent / "third_party" / "hermes",
            Path("/app/third_party/hermes"),
        ]
    )

    for candidate in candidates:
        if (candidate / "tui_gateway" / "entry.py").is_file():
            return candidate

    checked = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        "Hermes source root was not found. Set HERMES_GATEWAY_SRC_ROOT to the "
        f"Hermes checkout. Checked: {checked}"
    )


def _valid_python(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [
                executable,
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _choose_gateway_python(hermes_root: Path) -> str:
    candidates: list[str] = []
    for key in ("HERMES_GATEWAY_PYTHON", "HERMES_PYTHON"):
        value = (os.environ.get(key) or "").strip()
        if value:
            candidates.append(value)
    for venv_name in (".venv", "venv"):
        candidates.append(str(hermes_root / venv_name / "bin" / "python"))
    candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3", "python"):
        path = shutil.which(name)
        if path:
            candidates.append(path)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _valid_python(candidate):
            return candidate

    raise RuntimeError(
        "Hermes gateway requires Python 3.11 or newer. Set HERMES_GATEWAY_PYTHON "
        "to a compatible interpreter."
    )


def _ensure_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".hermes-write-test-{os.getpid()}"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _default_gateway_cwd(hermes_root: Path, hermes_home: Path | None = None) -> Path:
    configured = (os.environ.get("HERMES_GATEWAY_CWD") or os.environ.get("HERMES_CWD") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if _is_foundry_hosted():
        candidates = [Path.home() / "workspace"]
        if hermes_home is not None:
            candidates.append(hermes_home / "workspace")
        for workspace in candidates:
            if _ensure_writable_directory(workspace):
                return workspace
        checked = ", ".join(str(path) for path in candidates)
        raise RuntimeError(f"No writable Foundry workspace directory found. Checked: {checked}")
    if hermes_root.parent.name == "third_party":
        return hermes_root.parent.parent
    return Path.cwd()


def _gateway_child_env(hermes_root: Path, cwd: Path, hermes_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_PYTHON_SRC_ROOT"] = str(hermes_root)
    env["TERMINAL_CWD"] = str(cwd)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.pop("HERMES_TUI_BACKEND", None)
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = (
        f"{hermes_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(hermes_root)
    )
    return env


def _append_maintenance_history(path: Path, entry: dict[str, Any], max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return

    raw = path.read_bytes()
    kept = raw[-max_bytes:]
    newline = kept.find(b"\n")
    if newline >= 0:
        kept = kept[newline + 1 :]
    path.write_bytes(kept)


def _truncate_text(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _maintenance_event_payload(result: dict[str, Any]) -> dict[str, Any]:
    jobs = result.get("jobs")
    job_summaries: list[dict[str, Any]] = []
    if isinstance(jobs, list):
        for item in jobs:
            if not isinstance(item, dict):
                continue
            summary = {
                "name": item.get("name"),
                "status": item.get("status"),
                "duration_seconds": item.get("duration_seconds"),
            }
            if "error" in item:
                summary["error"] = item.get("error")
            if "reason" in item:
                summary["reason"] = item.get("reason")
            job_summaries.append(summary)
    return {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "duration_seconds": result.get("duration_seconds"),
        "history_path": result.get("history_path"),
        "jobs": job_summaries,
    }


def _payload_session_id(payload: dict[str, Any]) -> str:
    direct = str(payload.get("session_id") or "").strip()
    if direct:
        return direct
    session = payload.get("session")
    if isinstance(session, dict):
        return str(session.get("id") or "").strip()
    return ""


def _normalize_invoke_payload(payload: Any) -> Any:
    if not isinstance(payload, dict) or "kind" in payload or "input" not in payload:
        return payload

    routine_input = payload["input"]
    if isinstance(routine_input, dict):
        return routine_input
    if not isinstance(routine_input, str):
        return payload

    try:
        decoded = json.loads(routine_input)
    except json.JSONDecodeError:
        return payload
    return decoded if isinstance(decoded, dict) else payload


def _frame_event_type(frame: dict[str, Any]) -> str:
    if frame.get("method") != "event":
        return ""
    params = frame.get("params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("type") or "")


def _frame_session_id(frame: dict[str, Any]) -> str:
    params = frame.get("params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("session_id") or "")


class HermesChildBroker:
    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._buffers: dict[str, _EventBuffer] = {}
        self._ready: asyncio.Future[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def request(
        self,
        rpc_request: dict[str, Any],
        *,
        timeout: float = _RPC_RESPONSE_TIMEOUT_S,
        total_timeout: bool = False,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if total_timeout else None
        if deadline is None:
            await self._ensure_started()
        else:
            await self._ensure_started(ready_timeout=min(_GATEWAY_READY_TIMEOUT_S, timeout))
        rid = rpc_request.get("id")
        if rid is None:
            await self._write_request(rpc_request)
            return {"jsonrpc": "2.0", "result": {"status": "sent"}, "id": None}

        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rid] = future
        try:
            response_timeout = timeout
            if deadline is not None:
                response_timeout = deadline - loop.time()
                if response_timeout <= 0:
                    raise asyncio.TimeoutError
            await self._write_request(rpc_request)
            return await asyncio.wait_for(future, timeout=response_timeout)
        finally:
            self._pending.pop(rid, None)

    async def subscribe(
        self, session_id: str, since_seq: int = -1
    ) -> AsyncIterator[dict[str, Any]]:
        if not session_id:
            yield _jsonrpc_error(None, -32602, "session_id is required to subscribe to events")
            return

        await self._ensure_started()

        buf = self._buffers.get(session_id)
        if buf is None:
            buf = _EventBuffer(_event_buffer_capacity())
            self._buffers[session_id] = buf

        replay, last_dropped, queue = buf.open_subscription(since_seq)
        try:
            if since_seq < last_dropped:
                yield {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "replay.gap",
                        "session_id": session_id,
                        "seq": last_dropped,
                        "payload": {"missed_through": last_dropped},
                    },
                }
            for frame in replay:
                yield frame
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_RPC_STREAM_IDLE_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    yield {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": session_id,
                            "payload": {"message": "Hermes gateway stream timed out."},
                        },
                    }
                    return
                if item is _BUFFER_SHUTDOWN:
                    yield {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": session_id,
                            "payload": {"message": "Hermes gateway restarted."},
                        },
                    }
                    return
                if item is _BUFFER_OVERFLOW:
                    yield {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": session_id,
                            "payload": {
                                "message": (
                                    "Hermes gateway event stream fell behind; "
                                    "reconnect to resume from the last received seq."
                                )
                            },
                        },
                    }
                    return
                yield item
        finally:
            buf.close_subscription(queue)

    async def _ensure_started(self, *, ready_timeout: float = _GATEWAY_READY_TIMEOUT_S) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return

            hermes_root = _resolve_hermes_root()
            python = _choose_gateway_python(hermes_root)
            hermes_home = _prepare_child_hermes_home()
            cwd = _default_gateway_cwd(hermes_root, hermes_home)
            env = _gateway_child_env(hermes_root, cwd, hermes_home)

            self._pending.clear()
            for buf in self._buffers.values():
                buf.shutdown()
            self._buffers.clear()

            loop = asyncio.get_running_loop()
            self._ready = loop.create_future()
            self._proc = await asyncio.create_subprocess_exec(
                python,
                "-u",
                "-m",
                "tui_gateway.entry",
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                await asyncio.wait_for(self._ready, timeout=ready_timeout)
            except Exception:
                await self._stop_child()
                raise

    async def _write_request(self, rpc_request: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError("Hermes gateway child is not running.")

        line = json.dumps(rpc_request, ensure_ascii=False) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()

    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[hermes-child stdout] non-json frame: {line}", file=sys.stderr, flush=True)
                    continue
                if isinstance(frame, dict):
                    await self._route_frame(frame)
        finally:
            if proc.returncode is None:
                await proc.wait()
            if self._proc is proc:
                self._proc = None
            await self._fail_all(RuntimeError("Hermes gateway child exited."))

    async def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                print(f"[hermes-child] {line}", file=sys.stderr, flush=True)

    async def _route_frame(self, frame: dict[str, Any]) -> None:
        if _frame_event_type(frame) == "gateway.ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
            return

        rid = frame.get("id")
        if rid in self._pending:
            future = self._pending[rid]
            if not future.done():
                future.set_result(frame)
            return

        session_id = _frame_session_id(frame)
        if not session_id:
            return

        buf = self._buffers.get(session_id)
        if buf is None:
            buf = _EventBuffer(_event_buffer_capacity())
            self._buffers[session_id] = buf
        buf.append(frame)

    def emit_event(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> int | None:
        if not session_id:
            return None
        buf = self._buffers.get(session_id)
        if buf is None:
            buf = _EventBuffer(_event_buffer_capacity())
            self._buffers[session_id] = buf
        frame = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": event_type,
                "session_id": session_id,
                "payload": payload,
            },
        }
        return buf.append(frame)

    async def _fail_all(self, exc: Exception) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(exc)

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

        for buf in list(self._buffers.values()):
            buf.shutdown()
        self._buffers.clear()

    async def _stop_child(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


_broker = HermesChildBroker()


async def _handle_rpc(payload: dict[str, Any]):
    rpc_request = payload.get("request")
    if not isinstance(rpc_request, dict):
        return JSONResponse(
            {"error": "invalid_request", "message": "request must be a JSON-RPC object."},
            status_code=400,
        )

    method = str(rpc_request.get("method") or "")

    if method == "session.events":
        rid = rpc_request.get("id")
        params = rpc_request.get("params")
        if not isinstance(params, dict):
            params = {}
        session_id = str(params.get("session_id") or "")
        raw_since = params.get("since_seq")
        try:
            since_seq = int(raw_since) if raw_since is not None else -1
        except (TypeError, ValueError):
            since_seq = -1

        async def event_frames() -> AsyncIterator[str]:
            if not session_id:
                yield _sse_frame(_jsonrpc_error(rid, -32602, "session_id is required for session.events"))
                yield _sse_frame({"type": "done"})
                return

            yield _sse_frame(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "status": "subscribed",
                        "session_id": session_id,
                        "since_seq": since_seq,
                    },
                }
            )
            try:
                async for frame in _broker.subscribe(session_id, since_seq):
                    yield _sse_frame(frame)
                yield _sse_frame({"type": "done"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": session_id,
                            "payload": {"message": str(exc)},
                        },
                    }
                )
                yield _sse_frame({"type": "done"})

        return StreamingResponse(
            event_frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    try:
        response = await _broker.request(rpc_request)
    except Exception as exc:
        response = _jsonrpc_error(rpc_request.get("id"), 5000, str(exc))
    return JSONResponse(response)


async def _handle_maintenance(payload: dict[str, Any]):
    run_id = str(payload.get("run_id") or uuid.uuid4())
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not _maintenance_process_lock.acquire(blocking=False):
        skipped = {
            "kind": "hermes.maintenance.result",
            "run_id": run_id,
            "status": "skipped",
            "reason": "already_running",
            "started_at": started_at,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        telemetry.record_maintenance(skipped)
        return JSONResponse(skipped)

    try:
        hermes_home = _prepare_child_hermes_home()
        maintenance_dir = hermes_home / "foundry-maintenance"
        history_path = maintenance_dir / "history.jsonl"
        timeout = _positive_float(
            payload.get("timeout_seconds")
            or os.environ.get("HERMES_FOUNDRY_MAINTENANCE_TIMEOUT_SECONDS"),
            _DEFAULT_MAINTENANCE_TIMEOUT_S,
            minimum=5.0,
        )
        stale_lock_seconds = _positive_float(
            payload.get("stale_lock_seconds"),
            timeout * 2,
            minimum=timeout,
        )
        history_max_bytes = _positive_int(
            payload.get("history_max_bytes")
            or os.environ.get("HERMES_FOUNDRY_MAINTENANCE_HISTORY_MAX_BYTES"),
            _DEFAULT_MAINTENANCE_HISTORY_MAX_BYTES,
            minimum=1024,
        )
        request_payload = dict(payload)
        request_payload["run_id"] = run_id
        request_payload["started_at"] = started_at
        request_payload["timeout_seconds"] = timeout
        request_payload["stale_lock_seconds"] = stale_lock_seconds

        result = await _run_gateway_maintenance(request_payload, timeout)

        result.setdefault("kind", "hermes.maintenance.result")
        result.setdefault("run_id", run_id)
        result.setdefault("started_at", started_at)
        result.setdefault("ended_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if "history_path" not in result:
            result["history_path"] = str(history_path)

        try:
            _append_maintenance_history(history_path, result, history_max_bytes)
        except OSError as exc:
            result["history_error"] = str(exc)

        session_id = _payload_session_id(payload)
        if session_id:
            seq = _broker.emit_event(
                session_id,
                "maintenance.summary",
                _maintenance_event_payload(result),
            )
            if seq is not None:
                result["event_seq"] = seq

        telemetry.record_maintenance(result)
        return JSONResponse(result)
    except Exception as exc:
        errored = {
            "kind": "hermes.maintenance.result",
            "run_id": run_id,
            "status": "error",
            "started_at": started_at,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": str(exc),
        }
        telemetry.record_maintenance(errored)
        return JSONResponse(errored, status_code=500)
    finally:
        _maintenance_process_lock.release()


async def _run_gateway_maintenance(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    rpc_request = {
        "jsonrpc": "2.0",
        "id": f"maintenance:{payload.get('run_id')}",
        "method": "maintenance.run",
        "params": payload,
    }
    try:
        response = await _broker.request(rpc_request, timeout=timeout, total_timeout=True)
    except asyncio.TimeoutError:
        return {
            "kind": "hermes.maintenance.result",
            "run_id": payload.get("run_id"),
            "status": "error",
            "reason": "timeout",
            "started_at": payload.get("started_at"),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": f"maintenance timed out after {timeout:.0f}s",
        }

    if not isinstance(response, dict):
        return {
            "kind": "hermes.maintenance.result",
            "run_id": payload.get("run_id"),
            "status": "error",
            "started_at": payload.get("started_at"),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": "maintenance gateway returned a non-object response",
        }

    error = response.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "maintenance gateway returned an error")
        result = {
            "kind": "hermes.maintenance.result",
            "run_id": payload.get("run_id"),
            "status": "error",
            "started_at": payload.get("started_at"),
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": message,
        }
        if "code" in error:
            result["code"] = error["code"]
        return result

    result = response.get("result")
    if not isinstance(result, dict):
        result = {"status": "error", "error": "maintenance gateway returned non-object result"}
    result.setdefault("kind", "hermes.maintenance.result")
    result.setdefault("run_id", payload.get("run_id"))
    return result


@app.invoke_handler
async def handle_invoke(request: Request):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"error": "invalid_request", "message": "Request body is required."},
            status_code=400,
        )

    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError:
        payload = body.decode("utf-8", errors="replace")

    payload = _normalize_invoke_payload(payload)

    invocation_session_id = str(getattr(request.state, "session_id", "") or "").strip()

    if isinstance(payload, dict):
        if payload.get("kind") == "hermes.rpc":
            if invocation_session_id:
                routine_provisioner.schedule_maintenance_routine(invocation_session_id)
            return await _handle_rpc(payload)
        if payload.get("kind") == "hermes.maintenance":
            if invocation_session_id and not _payload_session_id(payload):
                payload["session_id"] = invocation_session_id
            return await _handle_maintenance(payload)

    return JSONResponse(
        {
            "error": "unsupported_payload",
            "message": (
                'This agent accepts Hermes RPC payloads: {"kind":"hermes.rpc","request":{...}} '
                'and maintenance payloads: {"kind":"hermes.maintenance",...}. '
                'Routine wrappers may provide either as {"input":"<json>"} or {"input":{...}}.'
            ),
        },
        status_code=400,
    )


if __name__ == "__main__":
    app.run()
