"""Risk classifier and Japanese summary generator for Claude Guard.

Pattern-matching based, no LLM dependency. Target: <50ms.

String-context aware: quoted string contents (echo args, JSON data, grep
patterns etc.) are stripped before pattern matching to prevent false positives
like `echo '{"command":"rm -rf dist/"}' | python3 script.py` from being
classified as high-risk. Indirect execution contexts (bash -c, eval, etc.)
are handled separately.
"""

import os
import re

from ipc_protocol import RISK_LOW, RISK_MEDIUM, RISK_HIGH

# --- Tool-level risk classification ---

LOW_RISK_TOOLS = frozenset({
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "TaskList", "TaskGet", "TaskOutput", "ToolSearch",
    # Nothing outside Claude Code changes when these run, so asking about
    # them is pure noise.
    "TodoWrite", "AskUserQuestion", "Skill", "NotebookRead",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
})

MEDIUM_RISK_TOOLS = frozenset({
    "Edit", "Write", "NotebookEdit", "Task", "Agent",
})

# MCP action-name prefixes that only read. An MCP tool whose action starts
# with one of these is treated as low risk instead of the medium default,
# which otherwise turns every Figma/Pencil lookup into an approval prompt.
MCP_READONLY_PREFIXES = (
    "get_", "list_", "read_", "search_", "find_", "fetch_",
    "query_", "describe_", "snapshot_", "screenshot_", "export_",
    "download_", "whoami", "check_", "inspect_", "view_",
)

# Tools that always require explicit approval
ALWAYS_HIGH_TOOLS = frozenset({
    "mcp__freee-mcp__freee_api_post",
    "mcp__freee-mcp__freee_api_put",
    "mcp__freee-mcp__freee_api_delete",
    "mcp__freee-mcp__freee_api_patch",
})

# --- Bash command patterns for high-risk detection ---

HIGH_RISK_BASH_PATTERNS = [
    # Destructive file operations
    (r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b', "再帰削除"),
    (r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*)\b', "強制削除"),
    (r'\bmkfs\b', "フォーマット"),
    (r'\bdd\b\s+', "ディスク書き込み"),
    # Git dangerous operations
    (r'\bgit\s+push\b', "Git プッシュ"),
    (r'\bgit\s+push\s+.*--force\b', "Git 強制プッシュ"),
    (r'\bgit\s+push\s+.*-f\b', "Git 強制プッシュ"),
    (r'\bgit\s+reset\s+--hard\b', "Git ハードリセット"),
    (r'\bgit\s+clean\s+.*-f\b', "Git 強制クリーン"),
    (r'\bgit\s+branch\s+.*-D\b', "Git ブランチ強制削除"),
    (r'\bgit\s+checkout\s+\.\s*$', "Git 変更全破棄"),
    (r'\bgit\s+restore\s+\.\s*$', "Git 変更全復元"),
    # Privilege escalation
    (r'\bsudo\b', "管理者権限実行"),
    # Process/system operations
    (r'\bkill\s+(-9|--signal\s+KILL)\b', "プロセス強制終了"),
    (r'\bkillall\b', "全プロセス終了"),
    (r'\bshutdown\b', "シャットダウン"),
    (r'\breboot\b', "再起動"),
    # Dangerous redirections
    (r'>\s*/dev/sd[a-z]', "デバイス直接書き込み"),
    (r':\(\)\s*\{\s*:\|:\s*&\s*\}', "フォークボム"),
    # Curl to shell
    (r'curl\s+.*\|\s*(ba)?sh\b', "リモートスクリプト実行"),
    (r'wget\s+.*\|\s*(ba)?sh\b', "リモートスクリプト実行"),
    # Package operations with sudo
    (r'\bsudo\s+(apt|yum|brew|pip|npm)\b', "管理者パッケージ操作"),
    # Database destructive
    (r'\bDROP\s+(TABLE|DATABASE)\b', "データベース削除"),
    (r'\bTRUNCATE\b', "テーブル切り詰め"),
    # Docker risky
    (r'\bdocker\s+system\s+prune\b', "Docker全削除"),
    (r'\bdocker\s+rm\s+.*-f\b', "Dockerコンテナ強制削除"),
]

MEDIUM_RISK_BASH_PATTERNS = [
    (r'\bnpm\s+install\b', "npmパッケージインストール"),
    (r'\bnpm\s+i\b', "npmパッケージインストール"),
    (r'\bpip3?\s+install\b', "pipパッケージインストール"),
    (r'\bgit\s+commit\b', "Gitコミット"),
    (r'\bgit\s+merge\b', "Gitマージ"),
    (r'\bgit\s+rebase\b', "Gitリベース"),
    (r'\bgit\s+stash\b', "Gitスタッシュ"),
    (r'\bgit\s+tag\b', "Gitタグ"),
    (r'\bgit\s+cherry-pick\b', "Gitチェリーピック"),
    (r'\bchmod\b', "パーミッション変更"),
    (r'\bchown\b', "所有者変更"),
    (r'\bmv\s+', "ファイル移動/リネーム"),
    (r'\bcp\s+', "ファイルコピー"),
    (r'\bmkdir\b', "ディレクトリ作成"),
    (r'\bdocker\s+build\b', "Dockerビルド"),
    (r'\bdocker\s+run\b', "Docker実行"),
    (r'\bdocker\s+compose\b', "Docker Compose"),
    (r'\bnpx\b', "npx実行"),
]

# Patterns detected on the STRIPPED command (quotes removed) that indicate
# indirect code execution. Even though the quoted content is stripped, the
# presence of these commands means the quoted args ARE executed.
INDIRECT_EXEC_HIGH_PATTERNS = [
    (r'\bbash\s+.*-c\b', "Bash間接実行"),
    (r'\bsh\s+.*-c\b', "Shell間接実行"),
    (r'\bzsh\s+.*-c\b', "Zsh間接実行"),
    (r'\beval\b', "eval実行"),
]


def _strip_string_literals(command: str) -> str:
    """Strip content from quoted string literals in a shell command.

    Uses a state machine to correctly handle:
    - Single quotes: no escape sequences, content is always literal
    - Double quotes: backslash escapes are recognized
    - Nested quotes are handled correctly

    Examples:
      echo '{"command":"rm -rf /"}' | python3 x.py
        → echo '' | python3 x.py

      rm -rf dist/
        → rm -rf dist/  (unchanged, no quotes)

      bash -c "rm -rf dist/"
        → bash -c ""  (quotes preserved, content stripped)

      grep "rm -rf" somefile.txt
        → grep "" somefile.txt
    """
    result = []
    i = 0
    length = len(command)

    while i < length:
        char = command[i]

        if char == "'":
            # Single-quoted string: no escaping, find closing quote
            result.append("'")
            i += 1
            while i < length and command[i] != "'":
                i += 1
            if i < length:
                result.append("'")  # closing quote
                i += 1
        elif char == '"':
            # Double-quoted string: handle backslash escapes
            result.append('"')
            i += 1
            while i < length and command[i] != '"':
                if command[i] == '\\' and i + 1 < length:
                    i += 2  # skip escaped character
                else:
                    i += 1
            if i < length:
                result.append('"')  # closing quote
                i += 1
        else:
            result.append(char)
            i += 1

    return ''.join(result)


def classify_bash_command(command: str) -> tuple[str, str | None]:
    """Classify a Bash command's risk level.

    Uses string-context-aware analysis:
    1. Strip quoted string contents to get the command "skeleton"
    2. Match risk patterns against the skeleton (avoids false positives
       from dangerous-looking strings inside echo/grep/JSON data)
    3. Detect indirect execution (bash -c, eval) on the skeleton, and if
       found, also classify the original quoted content

    Returns (risk_level, matched_description_ja).
    """
    stripped = _strip_string_literals(command)

    # Phase 1: Check high-risk patterns on stripped command
    for pattern, desc in HIGH_RISK_BASH_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            return RISK_HIGH, desc

    # Phase 2: Check if stripped command has indirect execution contexts.
    # If so, the original quoted content IS code being executed, so we
    # need to classify the full original command too.
    for pattern, desc in INDIRECT_EXEC_HIGH_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            # The command uses indirect execution - check the ORIGINAL
            # (unstripped) command for dangerous patterns inside quotes
            for hp, hdesc in HIGH_RISK_BASH_PATTERNS:
                if re.search(hp, command, re.IGNORECASE):
                    return RISK_HIGH, f"{desc}: {hdesc}"
            # Indirect execution itself is at least medium risk
            return RISK_MEDIUM, desc

    # Phase 3: Check medium-risk patterns on stripped command
    for pattern, desc in MEDIUM_RISK_BASH_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            return RISK_MEDIUM, desc

    return RISK_LOW, None


def classify_tool(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Classify a tool execution's risk level and generate a Japanese summary.

    Returns (risk_level, summary_ja).
    """
    # Check always-high tools first
    if tool_name in ALWAYS_HIGH_TOOLS:
        return RISK_HIGH, _summarize_mcp_tool(tool_name, tool_input)

    # Bash: classify by command content
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        risk, matched_desc = classify_bash_command(command)

        # Normalize whitespace for display: newlines break AppleScript dialogs
        # and menu items, so collapse the command to a single line first
        cmd_display = " ".join(command.split())
        cmd_short = cmd_display[:80] + ("..." if len(cmd_display) > 80 else "")

        if risk == RISK_HIGH:
            summary = f"⚠️ コマンド実行: {cmd_short}（{matched_desc}）"
        elif risk == RISK_MEDIUM:
            summary = f"コマンド実行: {cmd_short}（{matched_desc}）"
        else:
            summary = f"コマンド実行: {cmd_short}"

        return risk, summary

    # Low risk tools
    if tool_name in LOW_RISK_TOOLS:
        return RISK_LOW, _summarize_low_risk(tool_name, tool_input)

    # Medium risk tools
    if tool_name in MEDIUM_RISK_TOOLS:
        return RISK_MEDIUM, _summarize_medium_risk(tool_name, tool_input)

    # MCP tools: read-only actions are low risk, everything else medium
    if tool_name.startswith("mcp__"):
        if _is_mcp_readonly(tool_name):
            return RISK_LOW, _summarize_mcp_tool(tool_name, tool_input)
        return RISK_MEDIUM, _summarize_mcp_tool(tool_name, tool_input)

    # Unknown tools default to medium
    return RISK_MEDIUM, f"ツール実行: {tool_name}"


def _summarize_low_risk(tool_name: str, tool_input: dict) -> str:
    """Generate summary for low-risk tools."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        filename = os.path.basename(path) if path else "不明"
        return f"ファイル読み取り: {filename}"

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"ファイル検索: {pattern}"

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"コンテンツ検索: {pattern}"

    if tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"Web検索: {query[:50]}"

    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return f"Webフェッチ: {url[:50]}"

    return f"ツール実行: {tool_name}"


def _summarize_medium_risk(tool_name: str, tool_input: dict) -> str:
    """Generate summary for medium-risk tools."""
    if tool_name == "Edit":
        path = tool_input.get("file_path", "")
        filename = os.path.basename(path) if path else "不明"
        old = tool_input.get("old_string", "")
        if old:
            return f"ファイル編集: {filename}（コード置換）"
        return f"ファイル編集: {filename}"

    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        filename = os.path.basename(path) if path else "不明"
        return f"ファイル作成/上書き: {filename}"

    if tool_name == "NotebookEdit":
        return "ノートブック編集"

    if tool_name == "Task":
        desc = tool_input.get("description", "")
        return f"サブタスク: {desc[:50]}" if desc else "サブタスク実行"

    return f"ツール実行: {tool_name}"


def _is_mcp_readonly(tool_name: str) -> bool:
    """Whether an MCP tool name looks like a read-only operation."""
    parts = tool_name.split("__", 2)
    if len(parts) < 3:
        return False
    action = parts[2].lower()
    return action.startswith(MCP_READONLY_PREFIXES)


def _summarize_mcp_tool(tool_name: str, tool_input: dict) -> str:
    """Generate summary for MCP tools."""
    # Parse MCP tool name: mcp__server__tool_name
    parts = tool_name.split("__", 2)
    if len(parts) >= 3:
        server = parts[1]
        action = parts[2]
        # freee-specific summaries
        if "freee" in server:
            method = ""
            if "post" in action:
                method = "POST（データ作成）"
            elif "put" in action:
                method = "PUT（データ更新）"
            elif "delete" in action:
                method = "DELETE（データ削除）"
            elif "patch" in action:
                method = "PATCH（データ部分更新）"
            elif "get" in action:
                method = "GET（データ取得）"
            path = tool_input.get("path", "")
            return f"⚠️ freee API {method}: {path[:50]}" if method else f"freee API: {action}"
        return f"MCP {server}: {action}"

    return f"MCP ツール: {tool_name}"


# --- Plain-language descriptions for the approval dialog ---
#
# classify_tool() returns a one-line summary suitable for a menu row. The
# dialog needs more: what is about to happen, to what, and why we are asking.
# describe_tool() builds that, in language that doesn't assume the reader
# knows shell syntax.

# Keyed by the matched pattern description from the tables above.
# Value is (何をするか, どう影響するか).
PLAIN_BY_DESC = {
    # High risk
    "再帰削除": ("フォルダごとファイルを削除します",
                 "ゴミ箱に入らず、元に戻せません"),
    "強制削除": ("確認なしでファイルを削除します",
                 "ゴミ箱に入らず、元に戻せません"),
    "フォーマット": ("ディスクを初期化します", "ディスクの中身が全部消えます"),
    "ディスク書き込み": ("ディスクへ直接書き込みます",
                         "書き込み先を間違えるとデータが壊れます"),
    "Git プッシュ": ("変更をリモートリポジトリに送信します",
                     "他の人から見える場所に反映されます"),
    "Git 強制プッシュ": ("リモートの履歴を強制的に上書きします",
                         "他の人のコミットが消える可能性があります"),
    "Git ハードリセット": ("Gitを過去の状態に巻き戻します",
                           "コミットしていない変更は失われます"),
    "Git 強制クリーン": ("Git管理外のファイルを削除します",
                         "追跡していないファイルが消えます"),
    "Git ブランチ強制削除": ("ブランチを強制削除します",
                             "未マージの作業が失われる可能性があります"),
    "Git 変更全破棄": ("作業中の変更をすべて捨てます", "編集内容が失われます"),
    "Git 変更全復元": ("作業中の変更をすべて元に戻します", "編集内容が失われます"),
    "管理者権限実行": ("管理者権限（sudo）で実行します",
                       "システム全体に影響が及ぶ可能性があります"),
    "プロセス強制終了": ("動いているプロセスを強制終了します",
                         "保存していない作業が失われる可能性があります"),
    "全プロセス終了": ("同じ名前のプロセスをまとめて終了します",
                       "保存していない作業が失われる可能性があります"),
    "シャットダウン": ("Macをシャットダウンします", "作業中のアプリが終了します"),
    "再起動": ("Macを再起動します", "作業中のアプリが終了します"),
    "デバイス直接書き込み": ("ディスクデバイスへ直接書き込みます",
                             "データが壊れる可能性があります"),
    "フォークボム": ("システムを埋め尽くすコマンドです", "Macがフリーズします"),
    "リモートスクリプト実行": ("ネット上のスクリプトを取得してそのまま実行します",
                               "中身を検証していないコードが動きます"),
    "管理者パッケージ操作": ("管理者権限でパッケージを操作します",
                             "システム全体に影響が及ぶ可能性があります"),
    "データベース削除": ("テーブル/データベースを削除します",
                         "保存されたデータが消えます"),
    "テーブル切り詰め": ("テーブルの中身を全削除します", "保存されたデータが消えます"),
    "Docker全削除": ("Dockerのイメージ/コンテナをまとめて削除します",
                     "ビルド済みの環境が消えます"),
    "Dockerコンテナ強制削除": ("Dockerコンテナを強制削除します",
                               "コンテナ内のデータが消えます"),
    "Bash間接実行": ("組み立てた文字列をコマンドとして実行します",
                     "実行内容が一目で読み取りにくい形です"),
    "Shell間接実行": ("組み立てた文字列をコマンドとして実行します",
                      "実行内容が一目で読み取りにくい形です"),
    "Zsh間接実行": ("組み立てた文字列をコマンドとして実行します",
                    "実行内容が一目で読み取りにくい形です"),
    "eval実行": ("文字列をそのままコードとして実行します",
                 "実行内容が一目で読み取りにくい形です"),
    # Medium risk
    "npmパッケージインストール": ("npmパッケージを追加します",
                                  "node_modules と package.json が変わります"),
    "pipパッケージインストール": ("Pythonパッケージを追加します",
                                  "Python環境が変わります"),
    "Gitコミット": ("変更をGitに記録します", "手元の履歴が1つ増えます"),
    "Gitマージ": ("ブランチを統合します", "ファイルの内容が変わります"),
    "Gitリベース": ("コミット履歴を作り直します", "履歴の並びが変わります"),
    "Gitスタッシュ": ("作業中の変更を一時退避します",
                      "作業中のファイルが一旦元の状態に戻ります"),
    "Gitタグ": ("タグを操作します", "タグ情報が変わります"),
    "Gitチェリーピック": ("他ブランチのコミットを取り込みます",
                          "ファイルの内容が変わります"),
    "パーミッション変更": ("ファイルのアクセス権を変更します", "権限設定が変わります"),
    "所有者変更": ("ファイルの所有者を変更します", "権限設定が変わります"),
    "ファイル移動/リネーム": ("ファイルを移動または名前変更します",
                              "元の場所からファイルがなくなります"),
    "ファイルコピー": ("ファイルをコピーします",
                       "コピー先に同名ファイルがあれば上書きされます"),
    "ディレクトリ作成": ("フォルダを作ります", "新しいフォルダが増えます"),
    "Dockerビルド": ("Dockerイメージをビルドします", "時間とディスクを消費します"),
    "Docker実行": ("Dockerコンテナを起動します",
                   "バックグラウンドで動き続ける場合があります"),
    "Docker Compose": ("Docker Composeを実行します",
                       "複数のコンテナが起動/停止します"),
    "npx実行": ("npxで外部パッケージを実行します",
                "未インストールのパッケージが取得される場合があります"),
}

# Human-readable names for MCP servers.
MCP_SERVER_LABELS = {
    "figma": "Figma",
    "figma-remote-mcp": "Figma",
    "pencil": "Pencil",
    "freee-mcp": "freee（会計）",
    "claude-in-chrome": "Chrome",
    "plaud": "PLAUD",
    "claude_ai_Gmail": "Gmail",
    "claude_ai_Google_Calendar": "Googleカレンダー",
    "claude_ai_Google_Drive": "Googleドライブ",
}


def _short_path(path: str) -> tuple[str, str]:
    """Split a path into (filename, human-readable parent directory)."""
    if not path:
        return "不明", ""
    home = os.path.expanduser("~")
    parent = os.path.dirname(path)
    if parent.startswith(home):
        parent = "~" + parent[len(home):]
    return os.path.basename(path) or path, parent


def _first_line(text: str, limit: int = 60) -> str:
    """First meaningful line of a block of text, trimmed for display."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit] + ("…" if len(stripped) > limit else "")
    return ""


def describe_tool(tool_name: str, tool_input: dict, cwd: str = "") -> dict:
    """Build a plain-Japanese description of a pending tool call.

    Returns a dict with:
      headline — one line: what is about to happen
      fields   — list of (label, value) rows giving the specifics
      reason   — why Claude Guard is asking about it
    """
    tool_input = tool_input or {}
    _, cwd_display = _short_path(os.path.join(cwd, "x")) if cwd else ("", "")

    if tool_name == "Bash":
        return _describe_bash(tool_input, cwd_display)
    if tool_name in ("Edit", "MultiEdit"):
        return _describe_edit(tool_input)
    if tool_name == "Write":
        return _describe_write(tool_input)
    if tool_name == "NotebookEdit":
        return _describe_notebook(tool_input)
    if tool_name in ("Task", "Agent"):
        desc = tool_input.get("description") or tool_input.get("prompt", "")
        return {
            "headline": "別のエージェントを起動して作業させます",
            "fields": [("内容", _first_line(desc, 70) or "（説明なし）")],
            "reason": "エージェントが独自にファイル変更やコマンド実行を行うため確認しています",
        }
    if tool_name.startswith("mcp__"):
        return _describe_mcp(tool_name, tool_input)

    risk, summary = classify_tool(tool_name, tool_input)
    return {
        "headline": f"{tool_name} を実行します",
        "fields": [("内容", summary)],
        "reason": "Claude Guard が内容を判別できないツールのため確認しています",
    }


def _describe_bash(tool_input: dict, cwd_display: str) -> dict:
    command = tool_input.get("command", "")
    risk, matched = classify_bash_command(command)
    cmd_display = " ".join(command.split())
    cmd_short = cmd_display[:150] + ("…" if len(cmd_display) > 150 else "")

    plain = PLAIN_BY_DESC.get(matched or "")
    if plain:
        headline, impact = plain
    elif risk == RISK_HIGH:
        headline, impact = "注意が必要なコマンドを実行します", "影響範囲が広い可能性があります"
    else:
        headline, impact = "ターミナルでコマンドを実行します", "ファイルやシステムに変更が入る可能性があります"

    fields = [("コマンド", cmd_short)]
    if tool_input.get("description"):
        fields.insert(0, ("目的", _first_line(tool_input["description"], 70)))
    if cwd_display:
        fields.append(("作業場所", cwd_display))

    return {"headline": headline, "fields": fields, "reason": impact}


def _describe_edit(tool_input: dict) -> dict:
    path = tool_input.get("file_path", "")
    name, parent = _short_path(path)
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")

    fields = [("対象", name)]
    if parent:
        fields.append(("場所", parent))
    if old:
        fields.append(("変更前", _first_line(old)))
    if new:
        fields.append(("変更後", _first_line(new)))
    if tool_input.get("replace_all"):
        fields.append(("範囲", "一致する箇所すべて"))

    return {
        "headline": "既存ファイルの一部を書き換えます",
        "fields": fields,
        "reason": "ファイルの中身が変わるため確認しています",
    }


def _describe_write(tool_input: dict) -> dict:
    path = tool_input.get("file_path", "")
    name, parent = _short_path(path)
    content = tool_input.get("content", "")
    exists = bool(path) and os.path.exists(path)

    fields = [("対象", name)]
    if parent:
        fields.append(("場所", parent))
    fields.append(("分量", f"約{len(content.splitlines())}行"))

    if exists:
        headline = "既存ファイルを丸ごと上書きします"
        reason = "いまの内容が失われるため確認しています"
    else:
        headline = "新しいファイルを作ります"
        reason = "新しいファイルがディスクに書き込まれるため確認しています"

    return {"headline": headline, "fields": fields, "reason": reason}


def _describe_notebook(tool_input: dict) -> dict:
    path = tool_input.get("notebook_path", "")
    name, parent = _short_path(path)
    fields = [("対象", name)]
    if parent:
        fields.append(("場所", parent))
    return {
        "headline": "ノートブックのセルを書き換えます",
        "fields": fields,
        "reason": "ノートブックの中身が変わるため確認しています",
    }


def _describe_mcp(tool_name: str, tool_input: dict) -> dict:
    parts = tool_name.split("__", 2)
    server = parts[1] if len(parts) >= 2 else tool_name
    action = parts[2] if len(parts) >= 3 else ""
    label = MCP_SERVER_LABELS.get(server, server)

    fields = [("操作", action or tool_name)]

    # Surface the one or two arguments that identify what is being touched.
    for key in ("path", "url", "file_path", "fileKey", "nodeId", "name",
                "query", "prompt", "clientName"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            fields.append((key, _first_line(value, 60)))
        if len(fields) >= 3:
            break

    if "freee" in server:
        reason = "会計データが実際に作成・更新されるため確認しています"
        headline = f"{label} のデータを書き換えます"
    else:
        reason = f"{label} 側のデータが変わる可能性があるため確認しています"
        headline = f"{label} を操作します"

    return {"headline": headline, "fields": fields, "reason": reason}
