from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse


app = InvocationAgentServerHost()
_clarify_waiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[str]]] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_GATEWAY_READY_TIMEOUT_S = 45.0
_RPC_RESPONSE_TIMEOUT_S = 60.0
_RPC_STREAM_IDLE_TIMEOUT_S = 15 * 60.0
_RPC_STREAM_METHODS = frozenset({"prompt.submit"})
_TERMINAL_EVENT_TYPES = frozenset({"error", "message.complete"})


def _jsonrpc_error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _sse_frame(value: dict[str, Any]) -> str:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"


def _extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    for key in ("message", "input", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested = payload.get("input")
    if isinstance(nested, dict):
        value = nested.get("text") or nested.get("message")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _event_frame(event_type: str, payload: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"type": event_type}
    if payload is not None:
        body["payload"] = payload
    return _sse_frame(body)


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


def _default_child_hermes_home() -> Path:
    configured = _first_env("HERMES_CHILD_HOME", "HERMES_GATEWAY_HOME")
    if configured:
        return Path(configured).expanduser()

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
        self._streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._ready: asyncio.Future[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def request(self, rpc_request: dict[str, Any], *, timeout: float = _RPC_RESPONSE_TIMEOUT_S) -> dict[str, Any]:
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

    async def stream_request(self, rpc_request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        params = rpc_request.get("params")
        session_id = str(params.get("session_id") or "") if isinstance(params, dict) else ""
        if not session_id:
            yield _jsonrpc_error(rpc_request.get("id"), -32602, "session_id is required for streaming RPC")
            return

        await self._ensure_started()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        previous = self._streams.get(session_id)
        self._streams[session_id] = queue
        try:
            response = await self.request(rpc_request)
            yield response
            if response.get("error"):
                return

            while True:
                frame = await asyncio.wait_for(queue.get(), timeout=_RPC_STREAM_IDLE_TIMEOUT_S)
                yield frame
                if _frame_event_type(frame) in _TERMINAL_EVENT_TYPES:
                    return
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
        finally:
            if previous is not None:
                self._streams[session_id] = previous
            elif self._streams.get(session_id) is queue:
                self._streams.pop(session_id, None)

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
            for queue in self._streams.values():
                queue.put_nowait(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "payload": {"message": "Hermes gateway restarted."},
                        },
                    }
                )
            self._streams.clear()

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
        if session_id and session_id in self._streams:
            await self._streams[session_id].put(frame)

    async def _fail_all(self, exc: Exception) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(exc)

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

        for session_id, queue in list(self._streams.items()):
            queue.put_nowait(
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


async def _handle_control(payload: dict[str, Any]):
    control = payload.get("control")
    if not isinstance(control, dict):
        return JSONResponse(
            {"error": "invalid_request", "message": "control payload is required."},
            status_code=400,
        )

    control_type = str(control.get("type") or "").strip()
    if control_type == "clarify.respond":
        request_id = str(control.get("request_id") or "").strip()
        answer = str(control.get("answer") or "")
        waiter = _clarify_waiters.get(request_id)
        if not waiter:
            return JSONResponse(
                {"status": "not_found", "request_id": request_id},
                status_code=404,
            )
        loop, future = waiter
        if not future.done():
            loop.call_soon_threadsafe(future.set_result, answer)
        return JSONResponse({"status": "ok", "request_id": request_id})

    if control_type == "cancel":
        invocation_id = str(control.get("invocation_id") or "").strip()
        cancelled = False
        if invocation_id and (event := _cancel_events.get(invocation_id)):
            event.set()
            cancelled = True
        return JSONResponse(
            {
                "status": "cancelled" if cancelled else "not_found",
                "invocation_id": invocation_id,
            }
        )

    return JSONResponse(
        {"error": "unsupported_control", "message": f"Unsupported control type: {control_type}"},
        status_code=400,
    )


async def _handle_rpc(payload: dict[str, Any]):
    rpc_request = payload.get("request")
    if not isinstance(rpc_request, dict):
        return JSONResponse(
            {"error": "invalid_request", "message": "request must be a JSON-RPC object."},
            status_code=400,
        )

    method = str(rpc_request.get("method") or "")
    if method in _RPC_STREAM_METHODS:
        async def frames() -> AsyncIterator[str]:
            try:
                async for frame in _broker.stream_request(rpc_request):
                    yield _sse_frame(frame)
                yield _sse_frame({"type": "done"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session_id = ""
                params = rpc_request.get("params")
                if isinstance(params, dict):
                    session_id = str(params.get("session_id") or "")
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
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    try:
        response = await _broker.request(rpc_request)
    except Exception as exc:
        response = _jsonrpc_error(rpc_request.get("id"), 5000, str(exc))
    return JSONResponse(response)


async def _wait_for_clarify_answer(request_id: str, cancel_event: asyncio.Event) -> str | None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _clarify_waiters[request_id] = (loop, future)
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {future, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            if not future.done():
                future.cancel()
            return None
        cancel_task.cancel()
        return future.result()
    finally:
        _clarify_waiters.pop(request_id, None)


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

    if isinstance(payload, dict) and payload.get("kind") == "hermes.control":
        return await _handle_control(payload)

    text = _extract_text(payload)
    if not text:
        return JSONResponse(
            {
                "error": "invalid_request",
                "message": 'Send text directly or JSON with "message", "input", or "text".',
            },
            status_code=400,
        )

    session_payload = payload.get("session") if isinstance(payload, dict) else None
    session_id = (
        session_payload.get("workspace")
        if isinstance(session_payload, dict) and session_payload.get("workspace")
        else getattr(request.state, "session_id", "local")
    )
    invocation_id = (
        str(payload.get("invocation_id"))
        if isinstance(payload, dict) and payload.get("invocation_id")
        else getattr(request.state, "invocation_id", "local")
    )
    cancel_event = asyncio.Event()
    _cancel_events[invocation_id] = cancel_event

    response_text = (
        "Foundry local Hermes stub received your prompt:\n\n"
        f"> {text}\n\n"
        "This proves the Hermes TUI can route a turn through the local "
        "Azure AI Foundry Invocations host and render TUI-shaped events."
    )

    async def events() -> AsyncIterator[str]:
        try:
            yield _event_frame(
                "status.update",
                {
                    "kind": "info",
                    "text": f"Accepted Hermes invocation {invocation_id} for session {session_id}.",
                },
            )
            yield _event_frame("message.start", {})

            final_text = response_text
            if "clarify" in text.lower():
                request_id = f"clarify-{uuid4().hex[:8]}"
                yield _event_frame(
                    "clarify.request",
                    {
                        "request_id": request_id,
                        "question": "Which Foundry TUI control path should the stub demonstrate?",
                        "choices": ["session routing", "interrupt handling", "approval-style controls"],
                    },
                )
                answer = await _wait_for_clarify_answer(request_id, cancel_event)
                if answer is None or cancel_event.is_set():
                    yield _event_frame("done")
                    return
                yield _event_frame(
                    "status.update",
                    {
                        "kind": "info",
                        "text": f"Received clarification: {answer}",
                    },
                )
                final_text = (
                    "Foundry local Hermes stub received your clarification:\n\n"
                    f"> {answer}\n\n"
                    "The TUI displayed a clarify prompt, sent hermes.control back "
                    "through the proxy, and the same invocation continued."
                )

            for chunk in final_text.split(" "):
                if cancel_event.is_set():
                    yield _event_frame("done")
                    return
                yield _event_frame("message.delta", {"text": chunk + " "})
                await asyncio.sleep(0.08 if "slow" in text.lower() else 0.03)

            yield _event_frame(
                "message.complete",
                {
                    "status": "complete",
                    "text": final_text,
                    "usage": {
                        "calls": 1,
                        "input": len(text.split()),
                        "output": len(final_text.split()),
                        "total": len(text.split()) + len(final_text.split()),
                    },
                },
            )
            yield _event_frame("done")
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        finally:
            _cancel_events.pop(invocation_id, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    app.run()
