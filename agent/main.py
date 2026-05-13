from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse


app = InvocationAgentServerHost()
_GATEWAY_READY_TIMEOUT_S = 45.0
_RPC_RESPONSE_TIMEOUT_S = 60.0
_RPC_STREAM_IDLE_TIMEOUT_S = 15 * 60.0
_DEFAULT_EVENT_BUFFER_SIZE = 1000
_BUFFER_SHUTDOWN = object()
_BUFFER_OVERFLOW = object()


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


def _normalize_foundry_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        return ""

    lowered = base_url.lower()
    if lowered.endswith("/openai/v1"):
        return base_url
    if lowered.endswith("/openai"):
        return f"{base_url}/v1"
    if ".openai.azure.com" in lowered:
        return f"{base_url}/openai/v1"
    return base_url


def _foundry_child_model_config() -> dict[str, Any] | None:
    deployment_name = _first_env(
        "HERMES_FOUNDRY_MODEL_DEPLOYMENT_NAME",
        "AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_OPENAI_DEPLOYMENT",
    )
    base_url = _normalize_foundry_base_url(
        _first_env(
            "HERMES_FOUNDRY_BASE_URL",
            "AZURE_FOUNDRY_BASE_URL",
            "AZURE_OPENAI_ENDPOINT",
        )
    )
    if not deployment_name or not base_url:
        return None

    api_mode = _first_env(
        "HERMES_FOUNDRY_MODEL_API_MODE",
        "AZURE_FOUNDRY_MODEL_API_MODE",
        "HERMES_FOUNDRY_API_MODE",
    ) or "chat_completions"
    auth_mode = _first_env(
        "HERMES_FOUNDRY_AUTH_MODE",
        "AZURE_FOUNDRY_AUTH_MODE",
        "AZURE_FOUNDRY_MODEL_AUTH_MODE",
    ) or "default_azure_credential"

    return {
        "model": {
            "provider": "azure-foundry",
            "default": deployment_name,
            "base_url": base_url,
            "api_mode": api_mode,
            "auth_mode": auth_mode,
        }
    }


def _is_foundry_hosted() -> bool:
    return bool(os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT", "").strip())


def _default_child_hermes_home() -> Path:
    configured = _first_env("HERMES_CHILD_HOME", "HERMES_GATEWAY_HOME")
    if configured:
        return Path(configured).expanduser()

    if _is_foundry_hosted():
        return Path.home() / ".hermes"

    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache_root / "hermes-foundry-tui" / "hermes-home"


def _prepare_child_hermes_home() -> Path:
    hermes_home = _default_child_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)

    config = _foundry_child_model_config()
    if config is not None:
        config_path = hermes_home / "config.yaml"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
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


def _default_gateway_cwd(hermes_root: Path) -> Path:
    configured = (os.environ.get("HERMES_GATEWAY_CWD") or os.environ.get("HERMES_CWD") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if _is_foundry_hosted():
        workspace = Path.home() / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace
    if hermes_root.parent.name == "third_party":
        return hermes_root.parent.parent
    return Path.cwd()


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
    ) -> dict[str, Any]:
        await self._ensure_started()
        rid = rpc_request.get("id")
        if rid is None:
            await self._write_request(rpc_request)
            return {"jsonrpc": "2.0", "result": {"status": "sent"}, "id": None}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rid] = future
        try:
            await self._write_request(rpc_request)
            return await asyncio.wait_for(future, timeout=timeout)
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

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return

            hermes_root = _resolve_hermes_root()
            python = _choose_gateway_python(hermes_root)
            cwd = _default_gateway_cwd(hermes_root)
            hermes_home = _prepare_child_hermes_home()
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
                await asyncio.wait_for(self._ready, timeout=_GATEWAY_READY_TIMEOUT_S)
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

    if isinstance(payload, dict) and payload.get("kind") == "hermes.rpc":
        return await _handle_rpc(payload)

    return JSONResponse(
        {
            "error": "unsupported_payload",
            "message": 'This agent only accepts Hermes RPC payloads: {"kind":"hermes.rpc","request":{...}}.',
        },
        status_code=400,
    )


if __name__ == "__main__":
    app.run()
