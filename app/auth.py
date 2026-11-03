import base64
import hashlib
import hmac
import re

SESSION_ID_PATTERN = re.compile(r"^sesn_[A-Za-z0-9]+$")


def user_for_key(access_keys: dict[str, str], presented: str) -> str | None:
    """Return the user an access key belongs to, comparing in constant time."""
    user = None
    for key, name in access_keys.items():
        if hmac.compare_digest(key.encode(), presented.encode()):
            user = name
    return user


def _signature(secret: str, user: str, session_id: str) -> str:
    digest = hmac.new(secret.encode(), f"{user}\n{session_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode().rstrip("=")


# The browser holds a signed reference to its conversation, not the bare session ID, so one
# user can't continue (and read) another user's conversation by guessing or copying its ID.
def sign_session(secret: str, user: str, session_id: str) -> str:
    return f"{session_id}.{_signature(secret, user, session_id)}"


def verify_session(secret: str, user: str, token: str) -> str | None:
    """Return the session ID if the token was issued to this user, else None."""
    session_id, sep, signature = token.partition(".")
    if not sep or not SESSION_ID_PATTERN.match(session_id):
        return None
    if not hmac.compare_digest(signature, _signature(secret, user, session_id)):
        return None
    return session_id
