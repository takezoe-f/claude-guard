# Claude Guard

A macOS menu bar app for supervising [Claude Code](https://claude.com/claude-code) tool executions with a risk-based approval workflow.

**[日本語版 README](README.ja.md)**

Claude Code runs tools (Bash, Edit, Write, MCP calls, ...) as it works. Claude Guard hooks into every tool call and gives you a native supervision layer:

- **Risk-based approval flow** — low-risk reads pass silently, medium/high-risk operations pop a native approval dialog
- **Human-readable summaries** of every tool call in your menu bar
- **Execution history** in the menu bar dropdown
- **Defer button** — dismiss a dialog now, decide later from the menu bar
- **Autonomous mode** — one click to let unattended sessions run without dialogs (with full logging)
- **Fail-open by design** — Claude Guard crashing can never brick your Claude Code session

## How it works

```
Claude Code → PreToolUse hook → hook-client.py → Unix socket → claude-guard.py (menu bar)
                                                                    ↓
                                                          native approval dialog
                                                          [Deny] [Later] [Approve]
                                                                    ↓
Claude Code ← hook response ← hook-client.py ← Unix socket ← your decision
```

The risk classifier is pure pattern matching (no LLM call, <50ms), so the hook adds no noticeable latency.

## Risk classification

| Level | Examples | Behavior |
|-------|----------|----------|
| Low | Read, Glob, Grep, WebSearch | Auto-approved |
| Medium | Edit, Write, `npm install`, `git commit` | Dialog, 15s timeout → auto-approve |
| High | `rm -rf`, `git push`, `sudo`, destructive API calls | Dialog required, 30s timeout → auto-deny |

### String-context awareness

Quoted string contents (echo args, JSON payloads, grep patterns) are stripped before classification, so data that merely *mentions* a dangerous command doesn't trigger a false positive — while indirect execution is still caught:

```bash
# Low risk — "rm -rf" is just data inside an echo argument
echo '{"command":"rm -rf dist/"}' | python3 script.py

# High risk — bash -c actually executes the quoted payload
bash -c "rm -rf dist/"
```

## Approval dialog

| Button | Action |
|--------|--------|
| **Approve** | Allow the tool call |
| **Later** | Dismiss the dialog; the request is parked in the menu bar (icon turns 🛡🔶) for a later decision |
| **Deny** | Block the tool call — Claude receives the denial reason |

Deferred requests time out after 600s (configurable) and then follow the configured timeout action.

## Autonomous mode

For unattended sessions (overnight runs, autonomous skills):

- Toggle from the menu bar, or `touch ~/.claude/tools/claude-guard/autonomous.flag`
- All tool calls are auto-approved without dialogs; the icon changes to 🛡⚡ and every call is still logged to history
- Toggle off from the menu, or `rm .../autonomous.flag`

## Fail-open design

**Principle: a Claude Guard failure must never stall Claude Code.**

| Scenario | Behavior |
|----------|----------|
| Daemon not running | All tools auto-approved |
| Dialog frozen / force-quit | Treated as timeout, not as denial |
| hook-client crashes | Auto-approve (exit 0) |
| Socket failure | Auto-approve |

A denial only ever happens when you explicitly click **Deny**.

## Install

```bash
git clone https://github.com/takezoe-f/claude-guard.git ~/.claude/tools/claude-guard
cd ~/.claude/tools/claude-guard
bash install.sh
```

Or manually:

```bash
# 1. Install dependency
pip3 install rumps

# 2. Register the LaunchAgent (starts at login)
cp com.claude.guard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.claude.guard.plist

# 3. Add the PreToolUse hook to ~/.claude/settings.json
# {
#   "matcher": "",
#   "hooks": [{"type": "command", "command": "python3 ~/.claude/tools/claude-guard/hook-client.py"}]
# }
```

## Configuration (config.json)

```json
{
  "behavior": {
    "fail_mode": "open",
    "medium_risk_timeout_seconds": 15,
    "high_risk_timeout_seconds": 30,
    "timeout_action_medium": "approve",
    "timeout_action_high": "deny",
    "deferred_timeout_seconds": 600
  },
  "auto_approve_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
  "always_require_approval_tools": ["mcp__your-server__dangerous_tool"],
  "ui": {
    "show_low_risk_in_menu": false,
    "max_menu_items": 15
  }
}
```

- `auto_approve_tools` — tools that never show a dialog
- `always_require_approval_tools` — tools forced to high risk (e.g. write-access MCP tools for your accounting/CRM APIs)
- Edit directly via "Open settings..." in the menu

## Development

```bash
python3 -m unittest discover -s tests -v
```

## Files

```
claude-guard.py        # Menu bar app (rumps)
hook-client.py         # PreToolUse hook script
risk_classifier.py     # Risk classification + summaries (string-context aware)
ipc_protocol.py        # IPC constants & helpers
config.json            # Configuration
install.sh             # Installer
uninstall.sh           # Uninstaller
com.claude.guard.plist # LaunchAgent (start at login)
tests/                 # Unit tests
```

## Requirements

- macOS
- Python 3.11+
- [rumps](https://github.com/jaredks/rumps)

## License

MIT
