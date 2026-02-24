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
echo "📦 rumps をインストール中..."
pip3 install rumps --quiet 2>/dev/null || {
    echo "❌ rumps のインストールに失敗しました"
    exit 1
}
echo "✅ rumps インストール完了"

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
