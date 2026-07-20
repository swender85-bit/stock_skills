"""Tests for print_removal_contexts (KIK-470)."""

import os
from unittest.mock import patch

from scripts.common import print_removal_contexts


class TestPrintRemovalContexts:
    """Unit tests for print_removal_contexts."""

    def test_empty_list(self, capsys):
        """Empty symbol list produces no output."""
        print_removal_contexts([])
        assert capsys.readouterr().out == ""

    def test_with_context(self, capsys):
        """Symbols with graph context produce formatted output."""
        fake_result = {"context_markdown": "### 7203.T\nスクリーニング出現: 3回"}

        with patch(
            "src.data.context.auto_context.get_context", return_value=fake_result
        ):
            print_removal_contexts(["7203.T"])

        out = capsys.readouterr().out
        assert "売却候補のコンテキスト" in out
        assert "7203.T" in out
        assert "スクリーニング出現" in out

    def test_no_neo4j(self, capsys):
        """ImportError from auto_context is silently caught."""
        with patch.dict("sys.modules", {"src.data.context.auto_context": None}):
            print_removal_contexts(["AAPL"])
        # Graceful degradation — no output, no exception
        assert capsys.readouterr().out == ""

    def test_multiple_symbols(self, capsys):
        """Multiple symbols each get their context printed."""
        results = {
            "7203.T": {"context_markdown": "### 7203.T\n保有中"},
            "AAPL": {"context_markdown": "### AAPL\nウォッチ中"},
        }

        def fake_get_context(sym):
            return results.get(sym)

        with patch(
            "src.data.context.auto_context.get_context", side_effect=fake_get_context
        ):
            print_removal_contexts(["7203.T", "AAPL"])

        out = capsys.readouterr().out
        assert "7203.T" in out
        assert "AAPL" in out
        assert "売却候補のコンテキスト" in out

    def test_no_context_returned(self, capsys):
        """When get_context returns None, no output is produced."""
        with patch(
            "src.data.context.auto_context.get_context", return_value=None
        ):
            print_removal_contexts(["UNKNOWN"])
        assert capsys.readouterr().out == ""

    def test_exception_in_get_context(self, capsys):
        """Runtime error in get_context is silently caught."""
        with patch(
            "src.data.context.auto_context.get_context",
            side_effect=RuntimeError("DB down"),
        ):
            print_removal_contexts(["7203.T"])
        # Graceful degradation
        assert capsys.readouterr().out == ""


class TestCmdWhatIfCallsRemovalContexts:
    """Verify cmd_what_if integrates print_removal_contexts."""

    def test_source_has_removal_contexts_call(self):
        """run_portfolio.py source contains print_removal_contexts integration."""
        portfolio_script = os.path.join(
            os.path.dirname(__file__),
            "..",
            ".claude",
            "skills",
            "stock-portfolio",
            "scripts",
            "run_portfolio.py",
        )
        with open(portfolio_script, encoding="utf-8") as f:
            source = f.read()

        assert "print_removal_contexts" in source
        assert "removal_symbols" in source
        assert "KIK-470" in source
