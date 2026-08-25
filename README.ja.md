# Claude Guard

macOSメニューバーからClaude Codeのツール実行を対話的に管理するアプリケーション。

**[English README](README.md)**

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
                                                        [拒否] [今回だけ許可] [全部許可]
                                                                     ↓
Claude Code ← permissionDecision ← hook-client.py ← Unix Socket ← ユーザーの判断
```

hookは `permissionDecision` (`allow` / `deny`) をstdoutで返すため、**Claude Code標準の確認プロンプトは表示されない**。Claude Guardのダイアログが唯一の判断ポイントになる。

判断できない場合（デーモン停止、planモード、入力パース失敗）は何も出力せず終了し、Claude Code標準プロンプトに委ねる。勝手に承認はしない。

## リスク分類

| レベル | ツール例 | 動作 |
|--------|----------|------|
| 低 | Read, Glob, Grep, WebSearch, TodoWrite, 読み取り系MCP（`get_*` `list_*` `search_*` 等） | 自動承認（プロンプトなし） |
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

ツールが何をしようとしているかを、シェル構文を知らなくても読める日本語で表示する。

```
┌─ Claude Guard・中リスク（Write）──────────┐
│ 既存ファイルを丸ごと上書きします           │
│                                            │
│ 対象: settings.json                        │
│ 場所: ~/.claude                            │
│ 分量: 約42行                               │
│                                            │
│ 理由: いまの内容が失われるため確認しています │
│                                            │
│        [拒否] [今回だけ許可] [全部許可]     │
└────────────────────────────────────────────┘
```

- **headline** — 何をしようとしているか（「フォルダごとファイルを削除します」等）
- **fields** — 対象ファイル / コマンド / 作業場所などの具体
- **理由** — なぜ確認が入ったか、どう影響するか

### ボタン

macOSのダイアログはボタン最大3つ。3つ目はリスクによって入れ替わる。

| リスク | ボタン構成 | 既定ボタン |
|--------|------------|-----------|
| 中 | 拒否 / 今回だけ許可 / **全部許可** | 今回だけ許可 |
| 高 | 拒否 / 今回だけ許可 / **後で** | 拒否 |

`Esc` は「拒否」に割り当て。

| ボタン | 動作 |
|--------|------|
| **拒否** | ツール実行をブロック。理由がClaudeに伝わる |
| **今回だけ許可** | この1回だけ実行を許可 |
| **全部許可** | このセッションの中リスク以下を以後すべて自動許可（後述） |
| **後で** | ダイアログを閉じ、メニューバーに保留。後からメニューで承認/拒否 |

保留中はメニューバーアイコンが 🛡🔶 に変化し、保留アイテムのサブメニューから承認/拒否が可能。保留タイムアウト（デフォルト600秒）後はconfig設定に従い自動判定。

## セッション全許可

「全部許可」を押すと、Claude Codeの `session_id` に紐づけて許可が記録される（`sessions/<id>.json`）。

**スコープ:**
- 対象は **中リスクまで**。`rm -rf` / `git push` / `sudo` / freee API書き込みなどの**高リスクは毎回ダイアログが出る**
- **そのセッションのみ**。別ターミナルの別セッションには影響しない
- デフォルト12時間で自動失効（`session_allow.ttl_seconds`）

**解除:**
- メニューバー 🛡🔓 →「🔒 このセッションの全許可を解除」/「🔒 全許可をすべて解除」
- または `rm ~/.claude/tools/claude-guard/sessions/<session_id>.json`

`session_allow.max_risk` を `"high"` にすると高リスクも含めて許可される（非推奨）。`enabled: false` で機能自体を無効化し、従来の「後で」ボタンに戻せる。

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

**セッション全許可との違い:** 自律実行モードは**永続・全セッション・高リスク込み**。セッション全許可は**そのセッション限定・中リスクまで・12時間で失効**。日常作業では後者を使う。

## Fail-Open 設計

**原則: Claude Guardの障害でClaude Codeが止まることはない**

| シナリオ | 動作 |
|----------|------|
| デーモン未起動 | Claude Code標準プロンプトに委ねる |
| ダイアログがフリーズ/強制終了 | タイムアウトと同じ扱い（拒否にならない） |
| hook-client.pyが例外で落ちる | Claude Code標準プロンプトに委ねる |
| ソケット通信失敗 | Claude Code標準プロンプトに委ねる |
| planモード / bypassPermissions | 介入せずClaude Code側のルールに従う |

「拒否」は明示的に「拒否」ボタン（または `Esc`）を選んだ時のみ発生。

なお「fail-open」は**無言で承認することではない**。Claude Guardが判断できない状況では確認の責任をClaude Codeに戻すため、ユーザーが何も知らないまま実行されることはない。

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
  --- セッション全許可 (1件・中リスクまで) ---
  🔓 my-project（12分前〜） → [🔒 このセッションの全許可を解除]
  🔒 全許可をすべて解除
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
  "session_allow": {
    "enabled": true,
    "max_risk": "medium",
    "ttl_seconds": 43200
  },
  "auto_approve_tools": [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "TaskList", "TaskGet", "TaskOutput", "ToolSearch",
    "TodoWrite", "AskUserQuestion", "Skill"
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
├── risk_classifier.py     # リスク分類 + 日本語要約・説明文生成
├── session_state.py       # セッション全許可の記録/失効
├── ipc_protocol.py        # IPC定数・ヘルパー
├── config.json            # 設定ファイル
├── sessions/              # セッション全許可の記録（gitignore）
├── install.sh             # インストールスクリプト
├── uninstall.sh           # アンインストール
└── com.claude.guard.plist # LaunchAgent (ログイン時自動起動)
```

## アンインストール

```bash
bash uninstall.sh
```

## テスト

```bash
python3 tests/test_risk_classifier.py
python3 tests/test_session_state.py
python3 tests/test_hook_client.py
```

`test_hook_client.py` は hook-client.py をサブプロセスとして実行し、`permissionDecision` を検証する。デーモン稼働中や自律実行モード中は、実際のダイアログが出てしまうケースを自動でスキップする。

## 依存関係

- macOS
- Python 3.11+
- [rumps](https://github.com/jaredks/rumps) (pyobjc-core, pyobjc-framework-Cocoa)

## ライセンス

MIT
