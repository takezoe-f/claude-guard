#!/usr/bin/env python3
"""Claude Guard hook client - PreToolUse hook script.

Called by Claude Code's PreToolUse hook. Reads tool execution JSON from stdin,
classifies risk, and communicates with the Claude Guard daemon for approval.

The decision is returned to Claude Code as a PreToolUse permissionDecision on
stdout. This matters: exiting 0 with no output means "the hook had no opinion",
which leaves Claude Code's own permission prompt in place — so the user would
be asked twice, once by Claude Guard's dialog and once by Claude Code's raw
prompt. Emitting "allow"/"deny" makes Claude Guard the single place a decision
is made.

Three outcomes:
  allow       — Claude Code runs the tool without prompting
  deny        — Claude Code blocks the tool and tells Claude why
  passthrough — Claude Guard abstains; Claude Code's normal prompt appears
"""

import json
import os
import sys
import uuid

# Add the script's directory to Python path for local imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import session_state
from ipc_protocol import (
    MSG_LOG, MSG_REQUEST_APPROVAL,
    create_message, send_to_daemon, is_daemon_running,
)
from risk_classifier import classify_tool, RISK_LOW, RISK_MEDIUM, RISK_HIGH


def load_config() -> dict:
    """Load configuration from config.json."""
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(config_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "behavior": {
                "fail_mode": "open",
                "medium_risk_timeout_seconds": 15,
                "high_risk_timeout_seconds": 30,
                "timeout_action_medium": "approve",
                "timeout_action_high": "deny",
            },
            "auto_approve_tools": [],
            "always_require_approval_tools": [],
        }


def _emit(decision: str, reason: str):
    """Print a PreToolUse hook decision and exit."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))
    sys.exit(0)


def allow(reason: str = "Claude Guard により承認されました"):
    """Approve the tool call outright — no prompt from Claude Code."""
    _emit("allow", reason)


def deny(reason: str):
    """Block the tool call, telling Claude the reason."""
    _emit("deny", reason)


def passthrough():
    """Abstain, leaving Claude Code's own permission prompt in charge.

    Used when Claude Guard cannot make an informed decision (daemon down,
    plan mode, unparseable input). Never silently permits anything.
    """
    sys.exit(0)


def is_autonomous_mode() -> bool:
    """Check if autonomous mode is active (flag file exists)."""
    return os.path.exists(os.path.join(SCRIPT_DIR, "autonomous.flag"))


def log_to_daemon(tool_name: str, summary: str, risk: str, tool_input: dict):
    """Fire-and-forget history entry for the menu bar."""
    if not is_daemon_running():
        return
    msg = create_message(MSG_LOG, tool_name, summary, risk, tool_input)
    try:
        send_to_daemon(msg, timeout=1.0)
    except Exception:
        pass


def main():
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        # Any uncaught exception → hand back to Claude Code's own prompt
        # rather than approving on our behalf.
        passthrough()


def _main():
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Can't parse input — no basis for a decision
        passthrough()

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id", "")
    cwd = hook_input.get("cwd", "")
    permission_mode = hook_input.get("permission_mode", "")

    if not tool_name:
        passthrough()

    # Plan mode restricts tools on purpose, and bypassPermissions already
    # skips prompting. In both cases Claude Guard should stay out of the way
    # rather than override Claude Code's own rules.
    if permission_mode in ("plan", "bypassPermissions"):
        passthrough()

    config = load_config()

    # Autonomous mode: auto-approve everything, just log
    if is_autonomous_mode():
        risk, summary = classify_tool(tool_name, tool_input)
        log_to_daemon(tool_name, summary + " (自律実行)", risk, tool_input)
        allow("自律実行モードのため自動承認")

    # Check if tool is in auto-approve list
    if tool_name in config.get("auto_approve_tools", []):
        risk, summary = classify_tool(tool_name, tool_input)
        log_to_daemon(tool_name, summary, risk, tool_input)
        allow("自動承認リストに登録されたツール")

    # Classify risk
    risk, summary = classify_tool(tool_name, tool_input)

    # Check if tool always requires approval
    if tool_name in config.get("always_require_approval_tools", []):
        risk = RISK_HIGH

    if risk == RISK_LOW:
        # Low risk: nothing outside Claude Code changes — approve and log
        log_to_daemon(tool_name, summary, risk, tool_input)
        allow("低リスク（読み取り系）のため自動承認")

    # A session-wide grant from an earlier dialog. Deliberately capped at the
    # granted ceiling, so "全部許可" never covers rm -rf / git push / sudo.
    if session_state.allows(session_id, risk):
        log_to_daemon(tool_name, summary + " (セッション許可)", risk, tool_input)
        allow("このセッションで全部許可が選択されています")

    behavior = config.get("behavior", {})
    if risk == RISK_MEDIUM:
        timeout = behavior.get("medium_risk_timeout_seconds", 15)
    else:  # RISK_HIGH
        timeout = behavior.get("high_risk_timeout_seconds", 30)

    # No daemon means no dialog to show. Fall back to Claude Code's own
    # prompt — worse wording, but the user still gets a say.
    if not is_daemon_running():
        passthrough()

    # Send approval request to daemon.
    # Use a long timeout to accommodate deferred decisions from menu bar.
    deferred_timeout = behavior.get("deferred_timeout_seconds", 600)
    socket_timeout = float(max(timeout, deferred_timeout) + 10)

    request_id = str(uuid.uuid4())[:8]
    msg = create_message(
        MSG_REQUEST_APPROVAL, tool_name, summary, risk,
        tool_input, request_id,
    )
    msg["session_id"] = session_id
    msg["cwd"] = cwd

    response = send_to_daemon(msg, timeout=socket_timeout)

    if response is None:
        # Daemon accepted the request but never answered — don't guess.
        passthrough()

    reason = response.get("reason", "")
    if response.get("approved", False):
        allow(f"Claude Guard: {reason}" if reason else "Claude Guard により承認")
    else:
        deny(f"Claude Guard: {reason or 'ユーザーにより拒否されました'}")


if __name__ == "__main__":
    main()
