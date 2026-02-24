#!/bin/bash
# Claude Guard uninstaller
set -euo pipefail

GUARD_DIR="$HOME/.claude/tools/claude-guard"
PLIST_NAME="com.claude.guard.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
SOCKET_PATH="$GUARD_DIR/guard.sock"

echo "🛡 Claude Guard アンインストーラー"
echo "================================"

# 1. Stop and unload LaunchAgent
if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✅ LaunchAgent をアンロード・削除"
else
    echo "ℹ️  LaunchAgent は登録されていません"
fi

# 2. Kill running process
pkill -f "claude-guard.py" 2>/dev/null || true
echo "✅ プロセスを停止"

# 3. Remove socket file
if [ -S "$SOCKET_PATH" ]; then
    rm -f "$SOCKET_PATH"
    echo "✅ ソケットファイルを削除"
fi

echo ""
echo "================================"
echo "🛡 アンインストール完了！"
echo ""
echo "⚠️  以下は手動で行ってください:"
echo "  1. settings.json の PreToolUse から hook-client.py のエントリを削除"
echo "  2. 必要に応じて $GUARD_DIR を削除:"
echo "     rm -rf $GUARD_DIR"
echo ""
