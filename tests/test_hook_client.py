#!/usr/bin/env python3
"""Unit tests for the PreToolUse hook client's decision output.

The hook is run as a subprocess, the way Claude Code runs it. Only paths that
resolve without the daemon are covered here — anything that would reach the
daemon would pop a real approval dialog on the tester's screen.
"""

import json
import os
import subprocess
import sys
import unittest
import uuid

REPO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HOOK = os.path.join(REPO_DIR, "hook-client.py")

sys.path.insert(0, REPO_DIR)
import session_state  # noqa: E402
from ipc_protocol import is_daemon_running  # noqa: E402

PASSTHROUGH = "passthrough"


def run_hook(payload: dict) -> tuple[str, str]:
    """Run the hook and return (decision, reason).

    decision is "allow", "deny", or PASSTHROUGH when the hook produced no
    output — meaning it abstained and Claude Code's own prompt takes over.
    """
    result = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    out = result.stdout.strip()
    if not out:
        return PASSTHROUGH, ""
    block = json.loads(out)["hookSpecificOutput"]
    return block["permissionDecision"], block.get("permissionDecisionReason", "")


def autonomous_mode_active() -> bool:
    return os.path.exists(os.path.join(REPO_DIR, "autonomous.flag"))


@unittest.skipIf(autonomous_mode_active(),
                 "自律実行モードが有効なため全ツールが自動承認される")
class HookDecisionTests(unittest.TestCase):
    def payload(self, tool_name, tool_input=None, **extra):
        base = {
            "session_id": "test-" + uuid.uuid4().hex[:8],
            "cwd": "/tmp",
            "tool_name": tool_name,
            "tool_input": tool_input or {},
        }
        base.update(extra)
        return base

    def test_low_risk_tool_is_allowed_without_prompting(self):
        decision, _ = run_hook(
            self.payload("Read", {"file_path": "/tmp/a.txt"}))
        self.assertEqual(decision, "allow")

    def test_readonly_mcp_is_allowed(self):
        decision, _ = run_hook(self.payload("mcp__figma__get_metadata"))
        self.assertEqual(decision, "allow")

    def test_plan_mode_is_left_to_claude_code(self):
        # Plan mode restricts tools deliberately; the hook must not override it.
        decision, _ = run_hook(self.payload(
            "Bash", {"command": "npm install"}, permission_mode="plan"))
        self.assertEqual(decision, PASSTHROUGH)

    def test_bypass_permissions_is_left_to_claude_code(self):
        decision, _ = run_hook(self.payload(
            "Bash", {"command": "npm install"},
            permission_mode="bypassPermissions"))
        self.assertEqual(decision, PASSTHROUGH)

    def test_unparseable_input_does_not_approve(self):
        result = subprocess.run(
            [sys.executable, HOOK], input="not json",
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_missing_tool_name_does_not_approve(self):
        decision, _ = run_hook({"session_id": "x", "tool_input": {}})
        self.assertEqual(decision, PASSTHROUGH)

    def test_hook_always_exits_zero(self):
        # A non-zero exit would surface as a hook error in Claude Code.
        for payload in ("not json", json.dumps(self.payload("Read", {}))):
            result = subprocess.run(
                [sys.executable, HOOK], input=payload,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(autonomous_mode_active(),
                 "自律実行モードが有効なため全ツールが自動承認される")
class SessionGrantTests(unittest.TestCase):
    """These write to the real sessions/ directory using throwaway ids."""

    def setUp(self):
        self.session_id = "test-grant-" + uuid.uuid4().hex[:8]

    def tearDown(self):
        session_state.revoke(self.session_id)

    def payload(self, tool_name, tool_input):
        return {
            "session_id": self.session_id,
            "cwd": "/tmp",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }

    def test_granted_session_skips_the_dialog_for_medium_risk(self):
        session_state.grant(self.session_id, max_risk="medium")
        decision, reason = run_hook(
            self.payload("Bash", {"command": "npm install lodash"}))
        self.assertEqual(decision, "allow")
        self.assertIn("セッション", reason)

    @unittest.skipIf(is_daemon_running(),
                     "デーモン稼働中は承認ダイアログが表示されるためスキップ")
    def test_grant_does_not_cover_high_risk(self):
        session_state.grant(self.session_id, max_risk="medium")
        # Would otherwise reach the daemon; without a granted ceiling covering
        # high risk, the hook must not allow it outright.
        decision, _ = run_hook(
            self.payload("Bash", {"command": "git push origin main"}))
        self.assertNotEqual(decision, "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
