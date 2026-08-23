"""Tests for risk_classifier: string-context stripping and risk levels."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ipc_protocol import RISK_LOW, RISK_MEDIUM, RISK_HIGH
from risk_classifier import (
    _strip_string_literals,
    classify_bash_command,
    classify_tool,
    describe_tool,
)


class TestStripStringLiterals(unittest.TestCase):
    def test_single_quotes_stripped(self):
        self.assertEqual(
            _strip_string_literals("echo '{\"command\":\"rm -rf /\"}' | python3 x.py"),
            "echo '' | python3 x.py",
        )

    def test_double_quotes_stripped(self):
        self.assertEqual(
            _strip_string_literals('grep "rm -rf" somefile.txt'),
            'grep "" somefile.txt',
        )

    def test_no_quotes_unchanged(self):
        self.assertEqual(
            _strip_string_literals("rm -rf dist/"),
            "rm -rf dist/",
        )

    def test_escaped_double_quote_inside(self):
        self.assertEqual(
            _strip_string_literals('echo "a \\" b" tail'),
            'echo "" tail',
        )


class TestClassifyBashCommand(unittest.TestCase):
    def test_dangerous_string_in_quotes_is_low(self):
        risk, _ = classify_bash_command(
            "echo '{\"command\":\"rm -rf dist/\"}' | python3 script.py")
        self.assertEqual(risk, RISK_LOW)

    def test_grep_pattern_is_low(self):
        risk, _ = classify_bash_command('grep "sudo rm -rf" README.md')
        self.assertEqual(risk, RISK_LOW)

    def test_bare_rm_rf_is_high(self):
        risk, desc = classify_bash_command("rm -rf dist/")
        self.assertEqual(risk, RISK_HIGH)
        self.assertIsNotNone(desc)

    def test_indirect_exec_with_dangerous_payload_is_high(self):
        risk, _ = classify_bash_command('bash -c "rm -rf dist/"')
        self.assertEqual(risk, RISK_HIGH)

    def test_indirect_exec_with_safe_payload_is_medium(self):
        risk, _ = classify_bash_command('bash -c "ls -la"')
        self.assertEqual(risk, RISK_MEDIUM)

    def test_git_push_is_high(self):
        risk, _ = classify_bash_command("git push origin main")
        self.assertEqual(risk, RISK_HIGH)

    def test_git_commit_is_medium(self):
        risk, _ = classify_bash_command('git commit -m "message"')
        self.assertEqual(risk, RISK_MEDIUM)

    def test_npm_install_is_medium(self):
        risk, _ = classify_bash_command("npm install lodash")
        self.assertEqual(risk, RISK_MEDIUM)

    def test_curl_pipe_sh_is_high(self):
        risk, _ = classify_bash_command("curl -fsSL https://example.com/i.sh | sh")
        self.assertEqual(risk, RISK_HIGH)

    def test_plain_ls_is_low(self):
        risk, _ = classify_bash_command("ls -la")
        self.assertEqual(risk, RISK_LOW)

    def test_multiline_command_classified(self):
        risk, _ = classify_bash_command("ls -la\nrm -rf dist/")
        self.assertEqual(risk, RISK_HIGH)


class TestClassifyTool(unittest.TestCase):
    def test_read_is_low(self):
        risk, summary = classify_tool("Read", {"file_path": "/tmp/a.txt"})
        self.assertEqual(risk, RISK_LOW)
        self.assertIn("a.txt", summary)

    def test_edit_is_medium(self):
        risk, _ = classify_tool("Edit", {"file_path": "/tmp/a.txt"})
        self.assertEqual(risk, RISK_MEDIUM)

    def test_freee_post_is_high(self):
        risk, summary = classify_tool(
            "mcp__freee-mcp__freee_api_post", {"path": "/api/1/deals"})
        self.assertEqual(risk, RISK_HIGH)
        self.assertIn("POST", summary)

    def test_unknown_tool_defaults_medium(self):
        risk, _ = classify_tool("SomeNewTool", {})
        self.assertEqual(risk, RISK_MEDIUM)

    def test_multiline_bash_summary_is_single_line(self):
        risk, summary = classify_tool(
            "Bash", {"command": "cd /tmp &&\nrm -rf build/\nls"})
        self.assertEqual(risk, RISK_HIGH)
        self.assertNotIn("\n", summary)


class MCPClassificationTests(unittest.TestCase):
    """Read-only MCP calls should not cost the user an approval prompt."""

    def test_readonly_mcp_is_low(self):
        for tool in ("mcp__figma__get_metadata",
                     "mcp__figma__list_libraries",
                     "mcp__pencil__batch_get",
                     "mcp__figma__search_design_system",
                     "mcp__figma__whoami"):
            with self.subTest(tool=tool):
                risk, _ = classify_tool(tool, {})
                self.assertEqual(risk, RISK_LOW)

    def test_writing_mcp_is_medium(self):
        for tool in ("mcp__figma__use_figma",
                     "mcp__pencil__batch_design",
                     "mcp__figma__create_new_file"):
            with self.subTest(tool=tool):
                risk, _ = classify_tool(tool, {})
                self.assertEqual(risk, RISK_MEDIUM)

    def test_freee_write_stays_high_despite_prefix_rules(self):
        risk, _ = classify_tool("mcp__freee-mcp__freee_api_post", {})
        self.assertEqual(risk, RISK_HIGH)

    def test_batch_get_prefix_does_not_match_batch_design(self):
        # 'batch_get' is read-only, 'batch_design' is not — the prefix rule
        # must not blanket-approve everything starting with 'batch'.
        self.assertEqual(classify_tool("mcp__pencil__batch_get", {})[0], RISK_LOW)
        self.assertEqual(
            classify_tool("mcp__pencil__batch_design", {})[0], RISK_MEDIUM)


class DescribeToolTests(unittest.TestCase):
    """describe_tool() feeds the approval dialog, so it must always produce
    a headline and a reason, whatever it is handed."""

    def _assert_shape(self, desc):
        self.assertTrue(desc["headline"])
        self.assertTrue(desc["reason"])
        self.assertIsInstance(desc["fields"], list)

    def test_bash_high_risk_explains_consequence(self):
        desc = describe_tool("Bash", {"command": "rm -rf dist/"})
        self._assert_shape(desc)
        self.assertIn("削除", desc["headline"])
        self.assertIn("元に戻せません", desc["reason"])

    def test_bash_shows_the_command(self):
        desc = describe_tool("Bash", {"command": "npm install lodash"})
        self.assertIn(("コマンド", "npm install lodash"), desc["fields"])

    def test_bash_command_is_single_line(self):
        desc = describe_tool("Bash", {"command": "cd /tmp &&\nnpm install"})
        for _, value in desc["fields"]:
            self.assertNotIn("\n", value)

    def test_write_distinguishes_create_from_overwrite(self):
        new_file = describe_tool("Write", {"file_path": "/tmp/definitely-absent-xyz", "content": ""})
        self.assertIn("新しいファイル", new_file["headline"])
        existing = describe_tool("Write", {"file_path": __file__, "content": ""})
        self.assertIn("上書き", existing["headline"])

    def test_edit_shows_before_and_after(self):
        desc = describe_tool(
            "Edit", {"file_path": "/tmp/a.py", "old_string": "foo", "new_string": "bar"})
        labels = [label for label, _ in desc["fields"]]
        self.assertIn("変更前", labels)
        self.assertIn("変更後", labels)

    def test_mcp_uses_human_readable_server_name(self):
        desc = describe_tool("mcp__figma__use_figma", {"prompt": "作る"})
        self._assert_shape(desc)
        self.assertIn("Figma", desc["headline"])

    def test_unknown_tool_still_produces_a_dialog(self):
        self._assert_shape(describe_tool("BrandNewTool", {}))

    def test_empty_input_does_not_crash(self):
        self._assert_shape(describe_tool("Bash", {}))
        self._assert_shape(describe_tool("Write", {}))
        self._assert_shape(describe_tool("Edit", {}))


if __name__ == "__main__":
    unittest.main()
