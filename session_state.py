"""Session-scoped approval state for Claude Guard.

When the user clicks "全部許可" in an approval dialog, the grant is recorded
here keyed by Claude Code's session_id. Subsequent tool calls in the same
session skip the dialog entirely, up to the granted risk ceiling.

State lives in one small JSON file per session under sessions/. The hook
client reads it on every tool call (fast path, no socket round-trip); the
daemon writes it when the user grants.
"""

import json
import os
import re
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(SCRIPT_DIR, "sessions")

# Grants expire on their own so a machine left running for days doesn't keep
# honouring a grant from a session the user has long forgotten about.
DEFAULT_TTL_SECONDS = 12 * 3600

# Risk ordering, so "medium ceiling" can be compared against a request's risk.
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def risk_rank(risk: str) -> int:
    return _RISK_RANK.get(risk, 1)


def _safe_id(session_id: str) -> str:
    """Make a session id safe to use as a filename."""
    return _SAFE_ID.sub("_", str(session_id))[:64]


def _path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, _safe_id(session_id) + ".json")


def grant(session_id: str, max_risk: str = "medium",
          ttl_seconds: int = DEFAULT_TTL_SECONDS, cwd: str = "") -> bool:
    """Record a session-wide approval. Returns True on success."""
    if not session_id:
        return False
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        payload = {
            "session_id": session_id,
            "max_risk": max_risk,
            "granted_at": time.time(),
            "ttl_seconds": ttl_seconds,
            "cwd": cwd,
        }
        tmp = _path(session_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _path(session_id))
        return True
    except OSError:
        return False


def get(session_id: str) -> dict | None:
    """Return the active grant for a session, or None.

    Expired grants are deleted as a side effect so they can't come back.
    """
    if not session_id:
        return None
    path = _path(session_id)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    ttl = data.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if ttl and time.time() - data.get("granted_at", 0) > ttl:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    return data


def allows(session_id: str, risk: str) -> bool:
    """Whether an active grant covers a request at this risk level."""
    data = get(session_id)
    if not data:
        return False
    return risk_rank(risk) <= risk_rank(data.get("max_risk", "medium"))


def revoke(session_id: str) -> bool:
    """Drop a session's grant."""
    try:
        os.unlink(_path(session_id))
        return True
    except OSError:
        return False


def list_active() -> list[dict]:
    """All non-expired grants, newest first."""
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return []

    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, name)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        ttl = data.get("ttl_seconds", DEFAULT_TTL_SECONDS)
        if ttl and time.time() - data.get("granted_at", 0) > ttl:
            try:
                os.unlink(os.path.join(SESSIONS_DIR, name))
            except OSError:
                pass
            continue
        out.append(data)

    out.sort(key=lambda d: d.get("granted_at", 0), reverse=True)
    return out


def revoke_all() -> int:
    """Drop every grant. Returns how many were removed."""
    count = 0
    for data in list_active():
        if revoke(data.get("session_id", "")):
            count += 1
    return count
