import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def parse_access_keys(raw: str) -> dict[str, str]:
    """Parse APP_ACCESS_KEYS ("name:key,name:key") into {key: name}."""
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, key = entry.partition(":")
        name, key = name.strip(), key.strip()
        if not sep or not name or not key:
            raise ConfigError("APP_ACCESS_KEYS entries must look like name:key, separated by commas")
        if len(key) < 24:
            raise ConfigError(f"The access key for {name!r} is too short; use at least 24 characters")
        if key in keys:
            raise ConfigError(f"The access key for {name!r} is also used by {keys[key]!r}")
        keys[key] = name
    if not keys:
        raise ConfigError("Set APP_ACCESS_KEYS to at least one name:key pair")
    return keys


@dataclass(frozen=True)
class Settings:
    agent_id: str
    environment_id: str
    # Maps each access key to the name of the person or team it belongs to.
    access_keys: dict[str, str]
    # Signs the conversation references handed to the browser.
    app_secret: str
    requests_per_hour: int = 30
    max_concurrent_replies: int = 8
    reply_timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [
            name
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AGENT_ID", "ANTHROPIC_ENVIRONMENT_ID", "APP_ACCESS_KEYS", "APP_SECRET")
            if not os.getenv(name, "").strip()
        ]
        if missing:
            raise ConfigError(f"Missing required settings: {', '.join(missing)}. See .env.example.")

        app_secret = os.environ["APP_SECRET"].strip()
        if len(app_secret) < 32:
            raise ConfigError("APP_SECRET must be at least 32 characters")

        return cls(
            agent_id=os.environ["ANTHROPIC_AGENT_ID"].strip(),
            environment_id=os.environ["ANTHROPIC_ENVIRONMENT_ID"].strip(),
            access_keys=parse_access_keys(os.environ["APP_ACCESS_KEYS"]),
            app_secret=app_secret,
            requests_per_hour=_int("RATE_LIMIT_PER_HOUR", 30),
            max_concurrent_replies=_int("MAX_CONCURRENT_REPLIES", 8),
            reply_timeout_seconds=_int("REPLY_TIMEOUT_SECONDS", 300, minimum=30),
        )
