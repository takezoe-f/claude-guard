#!/usr/bin/env python3
"""Claude Guard - macOS menu bar app for Claude Code tool execution management.

A rumps-based menu bar application that:
- Listens on a Unix domain socket for tool execution events
- Shows tool execution history in the menu bar dropdown
- Displays approval dialogs for high-risk operations via osascript
- Auto-approves/denies based on timeout settings
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

# Add the script's directory to Python path for local imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import rumps

from ipc_protocol import (
    SOCKET_PATH, MSG_LOG, MSG_REQUEST_APPROVAL,
    RISK_LOW, RISK_MEDIUM, RISK_HIGH,
    encode_message, decode_message,
)

# --- Configuration ---

def load_config() -> dict:
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(config_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# --- osascript Dialog ---

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISION_DEFER = "defer"
DECISION_TIMEOUT = "timeout"


def show_approval_dialog(summary: str, risk: str, tool_name: str,
                         timeout_seconds: int) -> str:
    """Show a native macOS approval dialog using osascript.

    Uses 'tell current application' instead of 'tell application "System Events"'
    to avoid freezing issues. Falls back to DECISION_TIMEOUT on any error
    (crash, force-quit, kill) to maintain fail-open behavior.

    Returns one of: DECISION_APPROVE, DECISION_DENY, DECISION_DEFER, DECISION_TIMEOUT.
    """
    icon = "caution" if risk == RISK_HIGH else "note"

    # Escape special characters for AppleScript string
    safe_summary = (summary
                    .replace("\\", "\\\\")
                    .replace('"', '\\"'))

    risk_label = {"high": "高リスク", "medium": "中リスク", "low": "低リスク"}.get(risk, risk)

    applescript = f'''
    tell current application
        set dialogResult to display dialog "【{risk_label}】{safe_summary}" ¬
            buttons {{"拒否", "後で", "承認"}} ¬
            default button "承認" ¬
            with title "Claude Guard - {tool_name}" ¬
            with icon {icon} ¬
            giving up after {timeout_seconds}
        if gave up of dialogResult then
            return "timeout"
        else
            return button returned of dialogResult
        end if
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=timeout_seconds + 5,
        )

        output = result.stdout.strip()

        if result.returncode != 0:
            # Dialog was force-quit, killed, or crashed.
            # Treat as timeout (fail-open) rather than explicit deny.
            return DECISION_TIMEOUT

        if output == "承認":
            return DECISION_APPROVE
        if output == "後で":
            return DECISION_DEFER
        if output == "拒否":
            return DECISION_DENY
        if output == "timeout":
            return DECISION_TIMEOUT

        # Unknown output → fail-open
        return DECISION_TIMEOUT

    except subprocess.TimeoutExpired:
        return DECISION_TIMEOUT
    except Exception:
        # Any error (crash, signal, etc.) → fail-open
        return DECISION_TIMEOUT


# --- Menu Bar App ---

class ClaudeGuardApp(rumps.App):
    AUTONOMOUS_FLAG = os.path.join(SCRIPT_DIR, "autonomous.flag")

    def __init__(self):
        super().__init__(
            "Claude Guard",
            icon=None,
            title="🛡",
            quit_button=None,
        )
        self.config = load_config()
        self.history = []  # List of (timestamp, icon, summary, risk)
        self.pending_count = 0
        self.max_menu_items = self.config.get("ui", {}).get("max_menu_items", 15)
        self.show_low_risk = self.config.get("ui", {}).get("show_low_risk_in_menu", False)
        self.deferred_timeout = self.config.get("behavior", {}).get(
            "deferred_timeout_seconds", 600,
        )

        # Deferred requests: request_id -> {
        #   "summary": str, "risk": str, "tool_name": str,
        #   "event": threading.Event, "decision": str|None, "time": float
        # }
        self.deferred_requests = {}
        self._deferred_lock = threading.Lock()

        # Build initial menu
        self._update_title()
        self._rebuild_menu()

        # Start socket listener in background thread
        self.server_thread = threading.Thread(target=self._run_socket_server, daemon=True)
        self.server_thread.start()

    def _rebuild_menu(self):
        """Rebuild the menu bar dropdown."""
        self.menu.clear()

        # Deferred requests section (shown at top when items exist)
        with self._deferred_lock:
            deferred_items = list(self.deferred_requests.items())

        if deferred_items:
            deferred_header = rumps.MenuItem(f"--- 保留中 ({len(deferred_items)}件) ---")
            deferred_header.set_callback(None)
            self.menu.add(deferred_header)

            for req_id, req in deferred_items:
                # Create parent item with submenu for approve/deny
                risk_icon = "⚠️ " if req["risk"] == RISK_HIGH else ""
                parent = rumps.MenuItem(f"🔶 {risk_icon}{req['summary']}")
                parent.set_callback(None)

                approve_item = rumps.MenuItem(
                    "✅ 承認する",
                    callback=self._make_deferred_callback(req_id, DECISION_APPROVE),
                )
                deny_item = rumps.MenuItem(
                    "❌ 拒否する",
                    callback=self._make_deferred_callback(req_id, DECISION_DENY),
                )
                parent.add(approve_item)
                parent.add(deny_item)
                self.menu.add(parent)

            self.menu.add(rumps.separator)

        # History header
        header = rumps.MenuItem("--- 最近のツール実行 ---")
        header.set_callback(None)
        self.menu.add(header)

        # History items
        if not self.history:
            empty = rumps.MenuItem("  (まだ実行なし)")
            empty.set_callback(None)
            self.menu.add(empty)
        else:
            for _, icon, summary, _ in reversed(self.history[-self.max_menu_items:]):
                item = rumps.MenuItem(f"{icon} {summary}")
                item.set_callback(None)
                self.menu.add(item)

        self.menu.add(rumps.separator)

        # Pending count (dialog + deferred)
        total_pending = self.pending_count + len(deferred_items)
        pending_item = rumps.MenuItem(f"承認待ち: {total_pending}件")
        pending_item.set_callback(None)
        self.menu.add(pending_item)

        self.menu.add(rumps.separator)

        # Autonomous mode toggle
        autonomous = self._is_autonomous()
        auto_label = "✅ 自律実行モード (ON)" if autonomous else "⬜ 自律実行モード (OFF)"
        self.menu.add(rumps.MenuItem(auto_label, callback=self._toggle_autonomous))

        self.menu.add(rumps.separator)

        # Settings
        self.menu.add(rumps.MenuItem("設定を開く...", callback=self._open_config))
        self.menu.add(rumps.MenuItem("履歴をクリア", callback=self._clear_history))

        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Claude Guard を終了", callback=self._quit))

    def _add_history_entry(self, icon: str, summary: str, risk: str):
        """Add an entry to history and rebuild menu."""
        self.history.append((time.time(), icon, summary, risk))

        # Trim history
        if len(self.history) > 100:
            self.history = self.history[-100:]

        self._rebuild_menu()

    def _make_deferred_callback(self, request_id: str, decision: str):
        """Create a callback for deferred approve/deny menu items."""
        def callback(_):
            self._resolve_deferred(request_id, decision)
        return callback

    def _resolve_deferred(self, request_id: str, decision: str):
        """Resolve a deferred request from the menu bar."""
        with self._deferred_lock:
            req = self.deferred_requests.get(request_id)
            if not req:
                return
            req["decision"] = decision
            req["event"].set()  # Wake up the waiting thread

    def _is_autonomous(self) -> bool:
        """Check if autonomous mode is active."""
        return os.path.exists(self.AUTONOMOUS_FLAG)

    def _toggle_autonomous(self, _):
        """Toggle autonomous mode on/off."""
        if self._is_autonomous():
            os.unlink(self.AUTONOMOUS_FLAG)
        else:
            with open(self.AUTONOMOUS_FLAG, "w") as f:
                f.write(str(time.time()))
        self._update_title()
        self._rebuild_menu()

    def _update_title(self):
        """Update menu bar icon based on mode."""
        if self._is_autonomous():
            self.title = "🛡⚡"
        elif self.deferred_requests:
            self.title = "🛡🔶"
        else:
            self.title = "🛡"

    def _open_config(self, _):
        """Open config.json in default editor."""
        config_path = os.path.join(SCRIPT_DIR, "config.json")
        subprocess.Popen(["open", config_path])

    def _clear_history(self, _):
        """Clear execution history."""
        self.history.clear()
        self._rebuild_menu()

    def _quit(self, _):
        """Clean up and quit."""
        # Remove socket file
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
        rumps.quit_application()

    def _run_socket_server(self):
        """Run the Unix domain socket server in a background thread."""
        # Clean up stale socket
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(SOCKET_PATH)
        server.listen(5)
        server.settimeout(1.0)  # Allow periodic check for app shutdown

        while True:
            try:
                conn, _ = server.accept()
                # Handle each connection in a separate thread
                handler = threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    daemon=True,
                )
                handler.start()
            except socket.timeout:
                continue
            except Exception:
                continue

    def _handle_connection(self, conn: socket.socket):
        """Handle a single client connection."""
        # Use a long timeout to accommodate deferred decisions
        conn.settimeout(self.deferred_timeout + 30)
        try:
            buf = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break

            if not buf:
                return

            msg = decode_message(buf)
            msg_type = msg.get("type")
            tool_name = msg.get("tool_name", "")
            summary = msg.get("summary_ja", "")
            risk = msg.get("risk", RISK_LOW)

            if msg_type == MSG_LOG:
                # Just log it
                if risk != RISK_LOW or self.show_low_risk:
                    icon = "✅"
                    self._add_history_entry(icon, f"{summary} (自動承認)", risk)

            elif msg_type == MSG_REQUEST_APPROVAL:
                self.pending_count += 1
                self._rebuild_menu()

                try:
                    approved, reason = self._process_approval(
                        summary, risk, tool_name, msg,
                    )
                finally:
                    self.pending_count = max(0, self.pending_count - 1)
                    self._update_title()

                # Record in history
                if approved:
                    self._add_history_entry("✅", f"{summary} (承認済み)", risk)
                else:
                    self._add_history_entry("❌", f"{summary} (拒否)", risk)

                # Send response
                response = {"approved": approved, "reason": reason}
                conn.sendall(encode_message(response))

        except Exception as e:
            # On error, send approval (fail open)
            try:
                response = {"approved": True, "reason": f"エラー発生: {e}"}
                conn.sendall(encode_message(response))
            except Exception:
                pass
        finally:
            conn.close()

    def _process_approval(self, summary: str, risk: str, tool_name: str,
                          msg: dict) -> tuple[bool, str]:
        """Process an approval request.

        Shows a dialog first. If user clicks "後で", the request is deferred
        to the menu bar for later decision. The calling thread blocks until
        the user decides from the menu.

        Returns (approved, reason).
        """
        behavior = self.config.get("behavior", {})

        if risk == RISK_MEDIUM:
            timeout = behavior.get("medium_risk_timeout_seconds", 15)
            timeout_action = behavior.get("timeout_action_medium", "approve")
        else:  # RISK_HIGH
            timeout = behavior.get("high_risk_timeout_seconds", 30)
            timeout_action = behavior.get("timeout_action_high", "deny")

        # Show dialog
        decision = show_approval_dialog(summary, risk, tool_name, timeout)

        if decision == DECISION_APPROVE:
            return True, "ユーザーにより承認されました"

        if decision == DECISION_DENY:
            return False, "ユーザーにより拒否されました"

        if decision == DECISION_DEFER:
            return self._handle_deferred(
                summary, risk, tool_name, timeout_action,
            )

        # DECISION_TIMEOUT
        if timeout_action == "approve":
            return True, f"タイムアウト（{timeout}秒）- 自動承認"
        else:
            return False, f"タイムアウト（{timeout}秒）- 自動拒否"

    def _handle_deferred(self, summary: str, risk: str, tool_name: str,
                         timeout_action: str) -> tuple[bool, str]:
        """Handle a deferred approval request.

        Adds the request to the deferred queue and blocks until the user
        decides from the menu bar, or the deferred timeout expires.

        Returns (approved, reason).
        """
        request_id = str(uuid.uuid4())[:8]
        event = threading.Event()

        req = {
            "summary": summary,
            "risk": risk,
            "tool_name": tool_name,
            "event": event,
            "decision": None,
            "time": time.time(),
        }

        with self._deferred_lock:
            self.deferred_requests[request_id] = req

        self._update_title()
        self._rebuild_menu()

        # Block until user decides or timeout
        resolved = event.wait(timeout=self.deferred_timeout)

        # Clean up
        with self._deferred_lock:
            req = self.deferred_requests.pop(request_id, req)

        self._update_title()
        self._rebuild_menu()

        if not resolved:
            # Deferred timeout
            if timeout_action == "approve":
                return True, f"保留タイムアウト（{self.deferred_timeout}秒）- 自動承認"
            else:
                return False, f"保留タイムアウト（{self.deferred_timeout}秒）- 自動拒否"

        decision = req.get("decision")
        if decision == DECISION_APPROVE:
            return True, "メニューから承認されました"
        else:
            return False, "メニューから拒否されました"


if __name__ == "__main__":
    app = ClaudeGuardApp()
    app.run()
