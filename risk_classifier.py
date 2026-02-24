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
})

MEDIUM_RISK_TOOLS = frozenset({
    "Edit", "Write", "NotebookEdit", "Task", "TodoWrite",
})

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

        # Truncate command for display
        cmd_short = command[:80] + ("..." if len(command) > 80 else "")

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

    # MCP tools: default to medium unless in always-high list
    if tool_name.startswith("mcp__"):
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
