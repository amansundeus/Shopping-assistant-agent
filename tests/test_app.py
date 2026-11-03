import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import sign_session
from app.config import ConfigError, Settings, parse_access_keys
from app.limits import LimitExceeded, ReplyLimiter
from app.main import create_app

ALICE_KEY = "alice-key-0123456789abcdefghij"
BOB_KEY = "bob-key-0123456789abcdefghijklm"
SECRET = "s" * 40


def make_settings(**overrides) -> Settings:
    fields = dict(
        agent_id="agent_1",
        environment_id="env_1",
        access_keys={ALICE_KEY: "alice", BOB_KEY: "bob"},
        app_secret=SECRET,
        requests_per_hour=30,
        max_concurrent_replies=8,
        reply_timeout_seconds=300,
    )
    fields.update(overrides)
    return Settings(**fields)


def text_event(text):
    return SimpleNamespace(type="agent.message", content=[SimpleNamespace(type="text", text=text)])


def tool_event(name, tool_input):
    return SimpleNamespace(type="agent.tool_use", name=name, input=tool_input, id="tu_1", evaluated_permission="allow")


IDLE = SimpleNamespace(type="session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))


class FakeStream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        pass


class FakeClient:
    """Stands in for anthropic.Anthropic: records calls and replays a scripted turn."""

    def __init__(self, events=None):
        self.events = events if events is not None else [text_event("Here is your shortlist."), IDLE]
        self.created = []
        self.sent = []
        self.beta = SimpleNamespace(
            sessions=SimpleNamespace(
                create=self._create,
                events=SimpleNamespace(stream=self._stream, send=self._send),
            )
        )

    def _create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=f"sesn_{len(self.created)}")

    def _stream(self, session_id):
        return FakeStream(list(self.events))

    def _send(self, session_id, events):
        self.sent.append((session_id, events))


def sse_payloads(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def client(fake):
    return TestClient(create_app(make_settings(), client_factory=lambda: fake))


def auth(key=ALICE_KEY):
    return {"Authorization": f"Bearer {key}"}


def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"ok": True}


def test_page_is_served_with_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Shopping Assistant" in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": ALICE_KEY}])
def test_api_rejects_missing_or_bad_keys(client, headers):
    assert client.get("/api/me", headers=headers).status_code == 401
    assert client.post("/api/chat/stream", json={"prompt": "hi"}, headers=headers).status_code == 401


def test_me_returns_user(client):
    assert client.get("/api/me", headers=auth()).json() == {"user": "alice"}


def test_chat_streams_answer_and_signed_session(client, fake):
    fake.events = [tool_event("web_search", {"query": "5g phones"}), text_event("Pick the Redmi."), IDLE]
    response = client.post("/api/chat/stream", json={"prompt": "phone"}, headers=auth())
    payloads = sse_payloads(response)

    assert payloads[0]["type"] == "session"
    assert payloads[0]["session"] == sign_session(SECRET, "alice", "sesn_1")
    assert {"type": "tool", "name": "web_search", "detail": "5g phones"} in payloads
    assert {"type": "text", "text": "Pick the Redmi."} in payloads
    assert payloads[-1] == {"type": "done"}
    assert fake.created[0]["title"].startswith("[alice]")


def test_follow_up_reuses_session(client, fake):
    token = sign_session(SECRET, "alice", "sesn_42")
    response = client.post("/api/chat/stream", json={"prompt": "cheaper?", "session": token}, headers=auth())
    assert response.status_code == 200
    assert fake.created == []
    assert fake.sent[0][0] == "sesn_42"


@pytest.mark.parametrize(
    "token",
    [
        sign_session(SECRET, "bob", "sesn_42"),  # issued to someone else
        "sesn_42.forged",
        "sesn_42",
        sign_session("x" * 40, "alice", "sesn_42"),  # signed with another secret
    ],
)
def test_follow_up_rejects_tokens_not_issued_to_user(client, fake, token):
    response = client.post("/api/chat/stream", json={"prompt": "more", "session": token}, headers=auth())
    assert response.status_code == 400
    assert fake.sent == []


def test_hourly_limit(fake):
    client = TestClient(create_app(make_settings(requests_per_hour=2), client_factory=lambda: fake))
    for _ in range(2):
        assert client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth()).status_code == 200
    limited = client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth())
    assert limited.status_code == 429
    assert "limit" in limited.json()["detail"]
    # Other users have their own allowance.
    assert client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth(BOB_KEY)).status_code == 200


def test_limit_released_after_error(fake):
    fake.events = [SimpleNamespace(type="session.status_terminated")]
    client = TestClient(create_app(make_settings(), client_factory=lambda: fake))
    for _ in range(3):
        payloads = sse_payloads(client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth()))
        assert payloads[1]["type"] == "error"


def test_api_errors_are_not_leaked(fake):
    def boom(session_id):
        raise RuntimeError("internal detail sk-ant-secret")

    fake.beta.sessions.events.stream = boom
    client = TestClient(create_app(make_settings(), client_factory=lambda: fake))
    payloads = sse_payloads(client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth()))
    assert payloads[-1]["type"] == "error"
    assert "sk-ant" not in json.dumps(payloads)


def test_out_of_credit_is_explained(fake):
    import anthropic
    import httpx

    def no_credit(**kwargs):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions")
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=httpx.Response(400, request=request),
            body=None,
        )

    fake.beta.sessions.create = no_credit
    client = TestClient(create_app(make_settings(), client_factory=lambda: fake))
    response = client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth())
    assert response.status_code == 502
    assert "out of API credit" in response.json()["detail"]
    # The failed request doesn't hold the user's one-reply-at-a-time slot.
    assert client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth()).status_code == 502


def test_timeout_interrupts_agent(fake):
    release = threading.Event()

    class HangingStream(FakeStream):
        def __iter__(self):
            release.wait(5)
            raise RuntimeError("stream closed")

        def close(self):
            release.set()

    fake.beta.sessions.events.stream = lambda session_id: HangingStream([])
    client = TestClient(create_app(make_settings(reply_timeout_seconds=30), client_factory=lambda: fake))
    import app.main as main_module

    original = main_module._session_events
    main_module._session_events = lambda c, s, p, timeout: original(c, s, p, 0.2)
    try:
        payloads = sse_payloads(client.post("/api/chat/stream", json={"prompt": "x"}, headers=auth()))
    finally:
        main_module._session_events = original

    assert any(p["type"] == "error" and "too long" in p["error"] for p in payloads)
    assert any(events[0]["type"] == "user.interrupt" for _, events in fake.sent)


def test_limiter_one_reply_at_a_time_and_global_cap():
    limiter = ReplyLimiter(per_hour=10, max_concurrent=2)
    limiter.acquire("a")
    with pytest.raises(LimitExceeded, match="still being answered"):
        limiter.acquire("a")
    limiter.acquire("b")
    with pytest.raises(LimitExceeded, match="busy"):
        limiter.acquire("c")
    limiter.release("a")
    limiter.acquire("c")


def test_limiter_window_slides():
    now = [0.0]
    limiter = ReplyLimiter(per_hour=1, max_concurrent=5, clock=lambda: now[0])
    limiter.acquire("a")
    limiter.release("a")
    with pytest.raises(LimitExceeded):
        limiter.acquire("a")
    now[0] = 3600
    limiter.acquire("a")


def test_access_key_parsing():
    assert parse_access_keys(f"alice:{ALICE_KEY}, bob:{BOB_KEY}") == {ALICE_KEY: "alice", BOB_KEY: "bob"}
    for bad in ["", "alice", "alice:short", f"alice:{ALICE_KEY},bob:{ALICE_KEY}"]:
        with pytest.raises(ConfigError):
            parse_access_keys(bad)


def test_settings_require_secrets(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AGENT_ID", "ANTHROPIC_ENVIRONMENT_ID", "APP_ACCESS_KEYS", "APP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="Missing required settings"):
        Settings.from_env()
