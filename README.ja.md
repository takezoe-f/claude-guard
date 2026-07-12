# Claude Guard

macOSメニューバーからClaude Codeのツール実行を対話的に管理するアプリケーション。

## 概要

Claude Codeはツール（Bash, Edit, Write等）を自動実行しますが、Claude Guardを使うことで：

- 各ツール実行の内容を**日本語で要約**してメニューバーに表示
- **リスクレベルに応じた承認フロー**（低リスク=自動承認、高リスク=ダイアログ承認）
- **実行履歴**をメニューバードロップダウンで確認
- **自律実行モード**でスキル連携時はダイアログなしで全承認
- **保留機能**でダイアログを後回しにしてメニューバーから後で判断

## アーキテクチャ

```
Claude Code → PreToolUse Hook → hook-client.py → Unix Socket → claude-guard.py (メニューバー)
                                                                     ↓
                                                               osascript 承認ダイアログ
                                                               [承認] [後で] [拒否]
                                                                     ↓
Claude Code ← hook応答 ← hook-client.py ← Unix Socket ← ユーザーの判断
```

## リスク分類

| レベル | ツール例 | 動作 |
|--------|----------|------|
| 低 | Read, Glob, Grep, WebSearch | 自動承認 |
| 中 | Edit, Write, npm install, git commit | ダイアログ表示、15秒タイムアウト→自動承認 |
| 高 | rm -rf, git push, sudo, freee API書き込み | ダイアログ承認必須、30秒タイムアウト→自動拒否 |

### 文字列コンテキスト認識

クォート内のデータ（echo引数、JSONデータ、grepパターン等）は分類前にストリップされるため、偽陽性を防止。

```bash
# これは低リスク（echoの引数にrm -rfがあるだけ）
echo '{"command":"rm -rf dist/"}' | python3 script.py

# これは高リスク（bash -cの間接実行を検出）
bash -c "rm -rf dist/"
```

## 承認ダイアログ

ダイアログには3つの選択肢：

| ボタン | 動作 |
|--------|------|
| **承認** | ツール実行を許可 |
| **後で** | ダイアログを閉じ、メニューバーに保留。後からメニューで承認/拒否 |
| **拒否** | ツール実行をブロック |

保留中はメニューバーアイコンが 🛡🔶 に変化し、保留アイテムのサブメニューから承認/拒否が可能。保留タイムアウト（デフォルト600秒）後はconfig設定に従い自動判定。

## 自律実行モード

`autonomous-executor` スキルなどで確認なし実行する際に使用。

**有効化方法:**
- メニューバーの「⬜ 自律実行モード (OFF)」をクリック → ON
- またはスキル内で `touch ~/.claude/tools/claude-guard/autonomous.flag`

**動作:**
- 全ツール実行がダイアログなしで自動承認
- メニューバーアイコンが 🛡⚡ に変化
- 実行履歴に「(自律実行)」付きでログ記録

**無効化:**
- メニューバーの「✅ 自律実行モード (ON)」をクリック → OFF
- またはスキル内で `rm ~/.claude/tools/claude-guard/autonomous.flag`

## Fail-Open 設計

**原則: Claude Guardの障害でClaude Codeが止まることはない**

| シナリオ | 動作 |
|----------|------|
| デーモン未起動 | 全ツール自動承認 |
| ダイアログがフリーズ/強制終了 | タイムアウトと同じ扱い（拒否にならない） |
| hook-client.pyが例外で落ちる | 自動承認 (exit 0) |
| ソケット通信失敗 | 自動承認 |

「拒否」は明示的に「拒否」ボタンをクリックした時のみ発生。

## 再起動ループ防止

LaunchAgentの`KeepAlive`でクラッシュ時は自動再起動するが、致命的エラー（rumps未インストール等）の場合は `exit 0` で終了し再起動ループを回避。

| シナリオ | 動作 |
|----------|------|
| 正常起動 | 常駐 |
| rumps未インストール | exit 0 → 再起動しない |
| 実行中にクラッシュ | exit 1 → 30秒後に再起動 |
| 「終了」クリック | exit 0 → 再起動しない |

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

## 手動起動・確認

```bash
# 手動起動
python3 claude-guard.py &

# LaunchAgent経由
launchctl load ~/Library/LaunchAgents/com.claude.guard.plist

# 稼働確認
launchctl list | grep claude.guard
```

## メニュー構成

```
🛡 Claude Guard
  --- 保留中 (1件) ---
  🔶 ⚠️ rm -rf dist/ → [承認する] [拒否する]
  ---
  --- 最近のツール実行 ---
  ✅ Read: main.ts (自動承認)
  ✅ Edit: config.json (承認済み)
  ❌ Bash: rm -rf dist/ (拒否)
  ---
  承認待ち: 1件
  ---
  ⬜ 自律実行モード (OFF)
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
    "timeout_action_high": "deny",
    "deferred_timeout_seconds": 600
  },
  "auto_approve_tools": [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "TaskList", "TaskGet", "TaskOutput", "ToolSearch"
  ],
  "always_require_approval_tools": [
    "mcp__freee-mcp__freee_api_post",
    "mcp__freee-mcp__freee_api_put",
    "mcp__freee-mcp__freee_api_delete",
    "mcp__freee-mcp__freee_api_patch"
  ],
  "ui": {
    "show_low_risk_in_menu": false,
    "max_menu_items": 15
  }
}
```

頻繁に承認が面倒なツールは `auto_approve_tools` に追加すればダイアログが出なくなる。メニューの「設定を開く...」から直接編集可能。

## ファイル構成

```
~/.claude/tools/claude-guard/
├── claude-guard.py        # メニューバーアプリ本体 (rumps)
├── hook-client.py         # PreToolUseフックスクリプト
├── risk_classifier.py     # リスク分類 + 日本語要約（文字列コンテキスト認識）
├── ipc_protocol.py        # IPC定数・ヘルパー
├── config.json            # 設定ファイル
├── install.sh             # インストールスクリプト
├── uninstall.sh           # アンインストール
└── com.claude.guard.plist # LaunchAgent (ログイン時自動起動)
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
