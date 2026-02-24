#!/usr/bin/env python3
"""Claude Guard hook client - PreToolUse hook script.

Called by Claude Code's PreToolUse hook. Reads tool execution JSON from stdin,
classifies risk, and communicates with the Claude Guard daemon for approval.

Exit codes:
  0 = approved (or daemon unreachable in fail-open mode)
  2 = blocked (with JSON reason on stdout)
"""

import json
import os
import sys
import uuid

# Add the script's directory to Python path for local imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

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


def approve():
    """Exit with approval (no output needed for approve)."""
    sys.exit(0)


def block(reason: str):
    """Exit with block, outputting JSON reason."""
    result = {"decision": "block", "reason": reason}
    print(json.dumps(result))
    sys.exit(2)


def is_autonomous_mode() -> bool:
    """Check if autonomous mode is active (flag file exists)."""
    return os.path.exists(os.path.join(SCRIPT_DIR, "autonomous.flag"))


def main():
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Can't parse input, fail open
        approve()

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if not tool_name:
        approve()

    config = load_config()

    # Autonomous mode: auto-approve everything, just log
    if is_autonomous_mode():
        risk, summary = classify_tool(tool_name, tool_input)
        if is_daemon_running():
            msg = create_message(MSG_LOG, tool_name, summary + " (自律実行)", risk, tool_input)
            try:
                send_to_daemon(msg, timeout=1.0)
            except Exception:
                pass
        approve()

    # Check if tool is in auto-approve list
    auto_approve = config.get("auto_approve_tools", [])
    if tool_name in auto_approve:
        risk, summary = classify_tool(tool_name, tool_input)
        # Send log to daemon (non-blocking)
        if is_daemon_running():
            msg = create_message(MSG_LOG, tool_name, summary, risk, tool_input)
            try:
                send_to_daemon(msg, timeout=1.0)
            except Exception:
                pass
        approve()

    # Classify risk
    risk, summary = classify_tool(tool_name, tool_input)

    # Check if tool always requires approval
    always_require = config.get("always_require_approval_tools", [])
    if tool_name in always_require:
        risk = RISK_HIGH

    # Determine timeout based on risk
    behavior = config.get("behavior", {})

    if risk == RISK_LOW:
        # Low risk: auto-approve, just log
        if is_daemon_running():
            msg = create_message(MSG_LOG, tool_name, summary, risk, tool_input)
            try:
                send_to_daemon(msg, timeout=1.0)
            except Exception:
                pass
        approve()

    elif risk == RISK_MEDIUM:
        timeout = behavior.get("medium_risk_timeout_seconds", 15)
        timeout_action = behavior.get("timeout_action_medium", "approve")
    else:  # RISK_HIGH
        timeout = behavior.get("high_risk_timeout_seconds", 30)
        timeout_action = behavior.get("timeout_action_high", "deny")

    # Check if daemon is running
    if not is_daemon_running():
        fail_mode = behavior.get("fail_mode", "open")
        if fail_mode == "open":
            approve()
        else:
            block("Claude Guard デーモンが起動していません")

    # Send approval request to daemon
    request_id = str(uuid.uuid4())[:8]
    msg = create_message(
        MSG_REQUEST_APPROVAL, tool_name, summary, risk,
        tool_input, request_id,
    )

    response = send_to_daemon(msg, timeout=float(timeout + 2))

    if response is None:
        # Daemon didn't respond, use timeout action
        if timeout_action == "approve":
            approve()
        else:
            block(f"Claude Guard: タイムアウト（{timeout}秒）- 自動拒否")

    if response.get("approved", False):
        approve()
    else:
        reason = response.get("reason", "ユーザーにより拒否されました")
        block(f"Claude Guard: {reason}")


if __name__ == "__main__":
    main()
