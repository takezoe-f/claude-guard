# Claude Guard

macOSメニューバーからClaude Codeのツール実行を対話的に管理するアプリケーション。

## 概要

Claude Codeはツール（Bash, Edit, Write等）を自動実行しますが、Claude Guardを使うことで：

- 各ツール実行の内容を**日本語で要約**してメニューバーに表示
- **リスクレベルに応じた承認フロー**（低リスク=自動承認、高リスク=ダイアログ承認）
- **実行履歴**をメニューバードロップダウンで確認

## アーキテクチャ

```
Claude Code → PreToolUse Hook → hook-client.py → Unix Socket → claude-guard.py (メニューバー)
                                                                     ↓
                                                               osascript 承認ダイアログ
                                                                     ↓
Claude Code ← hook応答 ← hook-client.py ← Unix Socket ← ユーザーの判断
```

## リスク分類

| レベル | ツール例 | 動作 |
|--------|----------|------|
| 低 | Read, Glob, Grep, WebSearch | 自動承認 |
| 中 | Edit, Write, npm install, git commit | 通知+自動承認(15秒) |
| 高 | rm -rf, git push, sudo, freee API書き込み | ダイアログ承認必須(30秒タイムアウト→自動拒否) |

**文字列コンテキスト認識:** クォート内のデータ（echo引数、JSONデータ、grepパターン等）は分類前にストリップされるため、`echo '{"command":"rm -rf"}' | python3 script.py` のような偽陽性を防止。間接実行（`bash -c`, `eval`等）は別途検出。

## インストール

```bash
bash install.sh
```

または手動で：

```bash
# 1. 依存インストール
pip3 install rumps

# 2. LaunchAgent登録（ログイン時自動起動）
cp com.claude.guard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.claude.guard.plist

# 3. settings.json の PreToolUse にフック追加
# {
#   "matcher": "",
#   "hooks": [{"type": "command", "command": "python3 ~/.claude/tools/claude-guard/hook-client.py"}]
# }
```

## 手動起動

```bash
python3 claude-guard.py &
```

## メニュー構成

```
🛡 Claude Guard
  --- 最近のツール実行 ---
  ✅ Read: main.ts (自動承認)
  ✅ Edit: config.json (承認済み)
  ❌ Bash: rm -rf dist/ (拒否)
  ---
  承認待ち: 0件
  ---
  設定を開く...
  履歴をクリア
  ---
  Claude Guard を終了
```

## 設定 (config.json)

```json
{
  "behavior": {
    "fail_mode": "open",
    "medium_risk_timeout_seconds": 15,
    "high_risk_timeout_seconds": 30,
    "timeout_action_medium": "approve",
    "timeout_action_high": "deny"
  },
  "auto_approve_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
  "always_require_approval_tools": ["mcp__freee-mcp__freee_api_post"],
  "ui": {
    "show_low_risk_in_menu": false,
    "max_menu_items": 15
  }
}
```

## アンインストール

```bash
bash uninstall.sh
```

## 依存関係

- macOS
- Python 3.11+
- [rumps](https://github.com/jaredks/rumps) (pyobjc-core, pyobjc-framework-Cocoa)

## ライセンス

MIT
