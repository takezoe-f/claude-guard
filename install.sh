#!/bin/bash
# Claude Guard installer
set -euo pipefail

GUARD_DIR="$HOME/.claude/tools/claude-guard"
PLIST_NAME="com.claude.guard.plist"
PLIST_SRC="$GUARD_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
SETTINGS="$HOME/.claude/settings.json"

echo "🛡 Claude Guard インストーラー"
echo "================================"

# 1. Check Python3
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 が見つかりません。インストールしてください。"
    exit 1
fi
echo "✅ python3: $(python3 --version)"

# 2. Install rumps
# python3 -m pip で LaunchAgent が使う python3 と同じ環境に入れる。
# Homebrew Python 3.12+ は externally-managed で通常の pip install を拒否するため、
# 失敗時は --break-system-packages でリトライする（--user なのでシステムは汚さない）
echo "📦 rumps をインストール中..."
if python3 -c "import rumps" 2>/dev/null; then
    echo "✅ rumps は既にインストール済み"
elif python3 -m pip install --user rumps; then
    echo "✅ rumps インストール完了"
elif python3 -m pip install --user --break-system-packages rumps; then
    echo "✅ rumps インストール完了（externally-managed 環境）"
else
    echo "❌ rumps のインストールに失敗しました。上記のエラーを確認してください。"
    exit 1
fi

# 3. Make scripts executable
chmod +x "$GUARD_DIR/hook-client.py"
chmod +x "$GUARD_DIR/claude-guard.py"
echo "✅ スクリプトに実行権限を付与"

# 4. Install LaunchAgent (replace template placeholders with actual paths)
if [ -f "$PLIST_SRC" ]; then
    # Unload if already loaded
    launchctl unload "$PLIST_DST" 2>/dev/null || true

    PYTHON3_PATH="$(which python3)"
    sed -e "s|__PYTHON3_PATH__|${PYTHON3_PATH}|g" \
        -e "s|__GUARD_DIR__|${GUARD_DIR}|g" \
        "$PLIST_SRC" > "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "✅ LaunchAgent を登録（ログイン時に自動起動）"
else
    echo "⚠️  plist ファイルが見つかりません。手動起動してください。"
fi

# 5. Update settings.json with PreToolUse hook
if [ -f "$SETTINGS" ]; then
    # Check if hook-client.py is already registered
    if grep -q "hook-client.py" "$SETTINGS"; then
        echo "✅ settings.json にフックは既に登録済み"
    else
        echo "📝 settings.json にPreToolUseフックを追加します..."
        echo "   ※ 手動で以下を追加してください:"
        echo ""
        echo '   "PreToolUse" の配列に追加:'
        echo '   {'
        echo '     "matcher": "",'
        echo '     "hooks": [{'
        echo '       "type": "command",'
        echo '       "command": "python3 ~/.claude/tools/claude-guard/hook-client.py"'
        echo '     }]'
        echo '   }'
        echo ""
        echo "   ⚠️  既存のフックを壊さないよう、手動での追加を推奨します。"
    fi
else
    echo "⚠️  settings.json が見つかりません"
fi

echo ""
echo "================================"
echo "🛡 インストール完了！"
echo ""
echo "▶ メニューバーアプリを起動:"
echo "  python3 $GUARD_DIR/claude-guard.py"
echo ""
echo "▶ テスト:"
echo "  echo '{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"/tmp/test.txt\"}}' | python3 $GUARD_DIR/hook-client.py"
echo ""
