import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import anthropic
import anyio.to_thread
from anthropic import Anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.auth import sign_session, user_for_key, verify_session
from app.config import Settings
from app.limits import LimitExceeded, ReplyLimiter
from app.logging_setup import configure_logging

log = logging.getLogger("shopping_assistant")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# The agent's tools never pause for approval, but if a call ever does, nobody is watching
# to approve it, so it is denied rather than left waiting forever.
UNATTENDED_DENY_MESSAGE = "No one is available to approve this tool call. Continue without it."

# Scripts and styles come only from this app (plus Google Fonts); agent output can't load anything.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=5000)
    # The session token from an earlier reply, to ask a follow-up in the same conversation.
    session: str | None = Field(default=None, max_length=200)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _api_error_message(exc: Exception) -> str:
    """A message safe to show users; the full error goes to the log."""
    if isinstance(exc, anthropic.APIStatusError) and "credit balance" in str(exc.message).lower():
        return "The assistant is unavailable: the account it runs on is out of API credit. Let your admin know."
    if isinstance(exc, anthropic.RateLimitError):
        return "The assistant is busy right now. Wait a minute and try again."
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return "The AI service is having trouble right now. Try again in a minute."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the AI service. Try again in a minute."
    return "Something went wrong while answering. Try again, or start a new chat."


def _tool_activity(event: Any) -> dict[str, Any]:
    """Describe a tool call for the UI: the search query, or the site being read."""
    name = getattr(event, "name", "unknown_tool")
    tool_input = getattr(event, "input", None) or {}
    detail = ""
    if name == "web_search":
        detail = str(tool_input.get("query", ""))
    elif name == "web_fetch":
        detail = urlparse(str(tool_input.get("url", ""))).netloc
    return {"type": "tool", "name": name, "detail": detail}


def _session_events(client: Anthropic, session_id: str, prompt: str, timeout: float) -> Iterator[dict[str, Any]]:
    """Send the prompt to the session and yield simplified events until the agent's turn ends."""
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        # A reply that runs too long is cut off: closing the stream ends the loop below.
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            stream.close()

        watchdog = threading.Timer(timeout, on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            # Stream first, then send, so no early events are missed.
            client.beta.sessions.events.send(
                session_id=session_id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
            )
            for event in stream:
                if event.type == "agent.message":
                    for block in event.content:
                        if block.type == "text" and block.text:
                            yield {"type": "text", "text": block.text}
                elif event.type in ("agent.tool_use", "agent.mcp_tool_use"):
                    yield _tool_activity(event)
                    if getattr(event, "evaluated_permission", None) == "ask":
                        client.beta.sessions.events.send(
                            session_id=session_id,
                            events=[
                                {
                                    "type": "user.tool_confirmation",
                                    "tool_use_id": event.id,
                                    "result": "deny",
                                    "deny_message": UNATTENDED_DENY_MESSAGE,
                                }
                            ],
                        )
                elif event.type == "session.error":
                    log.error("session error", extra={"session_id": session_id, "error": str(getattr(event, "error", ""))})
                    yield {"type": "error", "error": "The assistant hit an error. Try again, or start a new chat."}
                elif event.type == "session.status_terminated":
                    yield {"type": "error", "error": "This conversation has ended. Start a new chat."}
                    return
                elif event.type == "session.status_idle":
                    stop_reason = getattr(getattr(event, "stop_reason", None), "type", None)
                    if stop_reason == "requires_action":
                        continue
                    if stop_reason not in (None, "end_turn"):
                        log.warning("session stopped", extra={"session_id": session_id, "stop_reason": stop_reason})
                        yield {"type": "error", "error": "The assistant stopped before finishing. Try asking again."}
                    return
        except Exception:
            if not timed_out.is_set():
                raise
        finally:
            watchdog.cancel()

        if timed_out.is_set():
            _interrupt(client, session_id)
            yield {"type": "error", "error": "This took too long, so it was stopped. Try a narrower question."}


def _interrupt(client: Anthropic, session_id: str) -> None:
    """Ask the agent to stop working, so a cut-off reply doesn't keep running up usage."""
    try:
        client.beta.sessions.events.send(session_id=session_id, events=[{"type": "user.interrupt"}])
    except Exception:  # noqa: BLE001
        log.warning("could not interrupt session", extra={"session_id": session_id}, exc_info=True)


def create_app(settings: Settings | None = None, client_factory: Callable[[], Anthropic] = Anthropic) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    client = client_factory()
    limiter = ReplyLimiter(settings.requests_per_hour, settings.max_concurrent_replies)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Each streaming reply holds a worker thread for its whole run.
        threads = anyio.to_thread.current_default_thread_limiter()
        threads.total_tokens = max(threads.total_tokens, settings.max_concurrent_replies * 2 + 10)
        yield

    app = FastAPI(title="Shopping Assistant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not request.url.path.startswith("/api/"):
            # Small static files: always revalidate, so a deploy shows up on the next load.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def current_user(request: Request) -> str:
        scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
        user = user_for_key(settings.access_keys, presented.strip()) if scheme.lower() == "bearer" else None
        if not user:
            raise HTTPException(status_code=401, detail="Your access key is missing or not valid. Sign in again.")
        return user

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/me")
    def me(user: str = Depends(current_user)) -> dict:
        return {"user": user}

    @app.post("/api/chat/stream")
    def chat_stream(req: ChatRequest, user: str = Depends(current_user)) -> StreamingResponse:
        session_id = None
        if req.session:
            session_id = verify_session(settings.app_secret, user, req.session)
            if not session_id:
                raise HTTPException(status_code=400, detail="This conversation can't be continued. Start a new chat.")

        try:
            limiter.acquire(user)
        except LimitExceeded as exc:
            log.info("limited", extra={"user": user, "reason": str(exc)})
            raise HTTPException(status_code=429, detail=str(exc)) from exc

        released = threading.Event()

        def release() -> None:
            if not released.is_set():
                released.set()
                limiter.release(user)

        new_session = session_id is None
        if new_session:
            try:
                session = client.beta.sessions.create(
                    agent={"type": "agent", "id": settings.agent_id},
                    environment_id=settings.environment_id,
                    title=f"[{user}] {req.prompt[:70]}",
                )
            except anthropic.APIError as exc:
                release()
                log.error("session create failed", extra={"user": user}, exc_info=True)
                raise HTTPException(status_code=502, detail=_api_error_message(exc)) from exc
            session_id = session.id

        def event_source() -> Iterator[str]:
            started = time.monotonic()
            tools: dict[str, int] = {}
            outcome = "ok"
            try:
                yield _sse({"type": "session", "session": sign_session(settings.app_secret, user, session_id)})
                for item in _session_events(client, session_id, req.prompt, settings.reply_timeout_seconds):
                    if item["type"] == "tool":
                        tools[item["name"]] = tools.get(item["name"], 0) + 1
                    elif item["type"] == "error":
                        outcome = "error"
                    yield _sse(item)
                yield _sse({"type": "done"})
            except GeneratorExit:
                # The browser left mid-reply; stop the agent rather than pay for an answer nobody reads.
                outcome = "client_disconnected"
                _interrupt(client, session_id)
                raise
            except Exception as exc:  # noqa: BLE001
                outcome = "failed"
                log.error("reply failed", extra={"user": user, "session_id": session_id}, exc_info=True)
                yield _sse({"type": "error", "error": _api_error_message(exc)})
            finally:
                release()
                # Prompts can hold personal details, so only their length is logged.
                log.info(
                    "reply",
                    extra={
                        "user": user,
                        "session_id": session_id,
                        "new_session": new_session,
                        "prompt_chars": len(req.prompt),
                        "tools": tools,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "outcome": outcome,
                    },
                )

        # The background task also releases the limit if the browser leaves before streaming starts.
        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(release),
        )

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
