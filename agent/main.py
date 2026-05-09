from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse


app = InvocationAgentServerHost()


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

    text = _extract_text(payload)
    if not text:
        return JSONResponse(
            {
                "error": "invalid_request",
                "message": 'Send text directly or JSON with "message", "input", or "text".',
            },
            status_code=400,
        )

    session_id = getattr(request.state, "session_id", "local")
    invocation_id = getattr(request.state, "invocation_id", "local")

    async def events() -> AsyncIterator[str]:
        started = {
            "type": "status.update",
            "payload": {
                "kind": "info",
                "text": f"Accepted Hermes invocation {invocation_id} for session {session_id}.",
            },
        }
        yield f"data: {json.dumps(started)}\n\n"
        yield f"data: {json.dumps({'type': 'message.start', 'payload': {}})}\n\n"
        complete = {
            "type": "message.complete",
            "payload": {
                "status": "incomplete",
                "text": f"Hermes hosted runtime is not wired yet. Received: {text}",
            },
        }
        yield f"data: {json.dumps(complete)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    app.run()
