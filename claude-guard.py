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
import queue
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

import session_state
from ipc_protocol import (
    SOCKET_PATH, MSG_LOG, MSG_REQUEST_APPROVAL,
    RISK_LOW, RISK_MEDIUM, RISK_HIGH,
    encode_message, decode_message,
)
from risk_classifier import describe_tool

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
DECISION_SESSION = "session"
DECISION_DENY = "deny"
DECISION_DEFER = "defer"
DECISION_TIMEOUT = "timeout"

BUTTON_DENY = "拒否"
BUTTON_ONCE = "今回だけ許可"
BUTTON_SESSION = "全部許可"
BUTTON_DEFER = "後で"

RISK_LABELS = {RISK_HIGH: "高リスク", RISK_MEDIUM: "中リスク", RISK_LOW: "低リスク"}


def _escape_applescript(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal.

    A raw line break inside an AppleScript string literal is a syntax error,
    so newlines are turned into the two-character escape \\n which AppleScript
    itself resolves back to a line break.
    """
    return (text
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n"))


def build_dialog_text(description: dict, risk: str) -> str:
    """Lay out the plain-Japanese body of the approval dialog.

    Shape:
        何をしようとしているか        <- headline
        (blank)
        対象: ...                     <- the specifics
        コマンド: ...
        (blank)
        理由: なぜ確認しているか
    """
    lines = [description.get("headline", "ツールを実行します"), ""]

    for label, value in description.get("fields", []):
        if value:
            lines.append(f"{label}: {value}")

    reason = description.get("reason", "")
    if reason:
        lines.append("")
        prefix = "⚠️ " if risk == RISK_HIGH else ""
        lines.append(f"{prefix}理由: {reason}")

    return "\n".join(lines)


def show_approval_dialog(description: dict, risk: str, tool_name: str,
                         timeout_seconds: int,
                         allow_session: bool = True) -> str:
    """Show a native macOS approval dialog using osascript.

    Uses 'tell current application' instead of 'tell application "System Events"'
    to avoid freezing issues. Falls back to DECISION_TIMEOUT on any unexpected
    error (crash, force-quit, kill) to maintain fail-open behavior.

    macOS dialogs allow at most three buttons, so the third slot is either the
    session-wide grant (medium risk) or the defer action (high risk, where a
    blanket grant must never be offered).

    Returns one of: DECISION_APPROVE, DECISION_SESSION, DECISION_DENY,
    DECISION_DEFER, DECISION_TIMEOUT.
    """
    icon = "caution" if risk == RISK_HIGH else "note"

    if allow_session:
        third, default_button = BUTTON_SESSION, BUTTON_ONCE
    else:
        third, default_button = BUTTON_DEFER, BUTTON_DENY

    body = _escape_applescript(build_dialog_text(description, risk))
    risk_label = RISK_LABELS.get(risk, risk)
    title = _escape_applescript(f"Claude Guard・{risk_label}（{tool_name}）")

    applescript = f'''
    tell current application
        set dialogResult to display dialog "{body}" ¬
            buttons {{"{BUTTON_DENY}", "{BUTTON_ONCE}", "{third}"}} ¬
            default button "{default_button}" ¬
            cancel button "{BUTTON_DENY}" ¬
            with title "{title}" ¬
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

        if result.returncode != 0:
            # A cancel button (拒否 / Esc) surfaces as AppleScript error -128,
            # which is a real decision and must not be mistaken for a crash.
            stderr = result.stderr or ""
            if "-128" in stderr or "User canceled" in stderr:
                return DECISION_DENY
            # Dialog was force-quit, killed, or crashed → treat as timeout.
            return DECISION_TIMEOUT

        output = result.stdout.strip()

        if output == BUTTON_ONCE:
            return DECISION_APPROVE
        if output == BUTTON_SESSION:
            return DECISION_SESSION
        if output == BUTTON_DEFER:
            return DECISION_DEFER
        if output == BUTTON_DENY:
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

        session_cfg = self.config.get("session_allow", {})
        self.session_allow_enabled = session_cfg.get("enabled", True)
        self.session_allow_max_risk = session_cfg.get("max_risk", RISK_MEDIUM)
        self.session_allow_ttl = session_cfg.get(
            "ttl_seconds", session_state.DEFAULT_TTL_SECONDS,
        )

        # Deferred requests: request_id -> {
        #   "summary": str, "risk": str, "tool_name": str,
        #   "event": threading.Event, "decision": str|None, "time": float
        # }
        self.deferred_requests = {}
        self._deferred_lock = threading.Lock()

        # UI dispatch queue: AppKit (menu/title) must only be touched from
        # the main thread. Socket handler threads enqueue closures here and
        # a rumps.Timer drains them on the main thread.
        self._ui_queue = queue.Queue()
        self._ui_timer = rumps.Timer(self._drain_ui_queue, 0.2)
        self._ui_timer.start()

        # Build initial menu
        self._update_title()
        self._rebuild_menu()

        # Start socket listener in background thread
        self.server_thread = threading.Thread(target=self._run_socket_server, daemon=True)
        self.server_thread.start()

    def _ui(self, fn):
        """Schedule a closure to run on the main thread (thread-safe)."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self, _):
        """Run queued UI updates on the main thread (rumps.Timer callback)."""
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass

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

        # Session-wide grants section
        grants = session_state.list_active()
        if grants:
            ceiling = RISK_LABELS.get(self.session_allow_max_risk, "中リスク")
            grants_header = rumps.MenuItem(
                f"--- セッション全許可 ({len(grants)}件・{ceiling}まで) ---")
            grants_header.set_callback(None)
            self.menu.add(grants_header)

            for grant in grants:
                sid = grant.get("session_id", "")
                label = os.path.basename(grant.get("cwd", "")) or sid[:8]
                elapsed = int((time.time() - grant.get("granted_at", 0)) / 60)
                parent = rumps.MenuItem(f"🔓 {label}（{elapsed}分前〜）")
                parent.set_callback(None)
                parent.add(rumps.MenuItem(
                    "🔒 このセッションの全許可を解除",
                    callback=self._make_revoke_callback(sid),
                ))
                self.menu.add(parent)

            self.menu.add(rumps.MenuItem(
                "🔒 全許可をすべて解除", callback=self._revoke_all_grants))
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

    def _make_revoke_callback(self, session_id: str):
        """Create a callback that revokes one session-wide grant."""
        def callback(_):
            session_state.revoke(session_id)
            self._update_title()
            self._rebuild_menu()
        return callback

    def _revoke_all_grants(self, _):
        """Revoke every session-wide grant."""
        session_state.revoke_all()
        self._update_title()
        self._rebuild_menu()

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
        elif session_state.list_active():
            self.title = "🛡🔓"
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
                    self._ui(lambda: self._add_history_entry(
                        "✅", f"{summary} (自動承認)", risk))

            elif msg_type == MSG_REQUEST_APPROVAL:
                def _inc():
                    self.pending_count += 1
                    self._rebuild_menu()
                self._ui(_inc)

                try:
                    approved, reason = self._process_approval(
                        summary, risk, tool_name, msg,
                    )
                finally:
                    def _dec():
                        self.pending_count = max(0, self.pending_count - 1)
                        self._update_title()
                    self._ui(_dec)

                # Record in history
                icon = "✅" if approved else "❌"
                label = "承認済み" if approved else "拒否"
                self._ui(lambda: self._add_history_entry(
                    icon, f"{summary} ({label})", risk))

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

        session_id = msg.get("session_id", "")
        cwd = msg.get("cwd", "")

        # A blanket grant is only ever offered for medium risk, and only when
        # we have a session to scope it to. High-risk calls keep asking.
        allow_session = (
            risk != RISK_HIGH
            and bool(session_id)
            and self.session_allow_enabled
        )

        description = describe_tool(tool_name, msg.get("tool_input", {}), cwd)
        decision = show_approval_dialog(
            description, risk, tool_name, timeout, allow_session,
        )

        if decision == DECISION_APPROVE:
            return True, "ユーザーにより承認されました"

        if decision == DECISION_SESSION:
            granted = session_state.grant(
                session_id,
                max_risk=self.session_allow_max_risk,
                ttl_seconds=self.session_allow_ttl,
                cwd=cwd,
            )
            self._ui(self._update_title)
            self._ui(self._rebuild_menu)
            if granted:
                ceiling = RISK_LABELS.get(self.session_allow_max_risk, "中リスク")
                return True, f"このセッションは{ceiling}まで自動許可に設定されました"
            return True, "ユーザーにより承認されました（セッション設定の保存に失敗）"

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

        self._ui(self._update_title)
        self._ui(self._rebuild_menu)

        # Block until user decides or timeout
        resolved = event.wait(timeout=self.deferred_timeout)

        # Clean up
        with self._deferred_lock:
            req = self.deferred_requests.pop(request_id, req)

        self._ui(self._update_title)
        self._ui(self._rebuild_menu)

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


def _preflight_check():
    """Verify dependencies and environment before starting.

    If a fatal error is found, log it and exit 0 (successful exit)
    so KeepAlive does NOT restart us into a crash loop.
    Exit code 1 (error) would trigger KeepAlive restart.
    """
    errors = []

    try:
        import rumps  # noqa: F401
    except ImportError:
        errors.append("rumps がインストールされていません (pip3 install rumps)")

    socket_dir = os.path.dirname(SOCKET_PATH)
    if not os.path.isdir(socket_dir):
        errors.append(f"ディレクトリが存在しません: {socket_dir}")

    if errors:
        for e in errors:
            print(f"[Claude Guard] 起動エラー: {e}", file=sys.stderr)
        print("[Claude Guard] 致命的エラーのため終了します（再起動ループ防止）",
              file=sys.stderr)
        sys.exit(0)  # exit 0 = KeepAlive won't restart


if __name__ == "__main__":
    _preflight_check()
    app = ClaudeGuardApp()
    app.run()
