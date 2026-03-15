"""Unit tests for summarize_diff() and progress() in utils.py."""

from __future__ import annotations

from unittest.mock import patch

from millstone.utils import progress, summarize_diff

# ---------------------------------------------------------------------------
# summarize_diff tests
# ---------------------------------------------------------------------------

MULTI_FILE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def foo():
-    pass
+    return 1
diff --git a/src/bar.py b/src/bar.py
--- a/src/bar.py
+++ b/src/bar.py
@@ -5,2 +5,3 @@
 x = 1
+y = 2
+z = 3
"""


class TestSummarizeDiffMultiFile:
    def test_file_count(self):
        result = summarize_diff(MULTI_FILE_DIFF)
        assert "2 file(s)" in result

    def test_line_counts(self):
        # +import sys, +return 1, -pass, +y = 2, +z = 3  → added=4, removed=1
        result = summarize_diff(MULTI_FILE_DIFF)
        assert "+4/-1" in result
        assert "(5 total)" in result

    def test_per_file_stats(self):
        result = summarize_diff(MULTI_FILE_DIFF)
        assert "src/foo.py: +2/-1" in result
        assert "src/bar.py: +2/-0" in result


class TestSummarizeDiffEmpty:
    def test_empty_string(self):
        assert summarize_diff("") == "(empty diff)"

    def test_whitespace_only(self):
        assert summarize_diff("   \n\n  ") == "(empty diff)"

    def test_none_input(self):
        # None should not crash — guard returns "(empty diff)" via falsy check
        assert summarize_diff(None) == "(empty diff)"  # type: ignore[arg-type]


RENAME_ONLY_DIFF = """\
diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""


class TestSummarizeDiffRename:
    def test_rename_only_counted_as_file(self):
        result = summarize_diff(RENAME_ONLY_DIFF)
        assert "1 file(s)" in result

    def test_rename_zero_changes(self):
        result = summarize_diff(RENAME_ONLY_DIFF)
        assert "+0/-0" in result


BINARY_DIFF = """\
diff --git a/image.png b/image.png
Binary files a/image.png and b/image.png differ
"""


class TestSummarizeDiffBinary:
    def test_binary_counted_as_file(self):
        result = summarize_diff(BINARY_DIFF)
        assert "1 file(s)" in result

    def test_binary_zero_line_changes(self):
        result = summarize_diff(BINARY_DIFF)
        assert "+0/-0" in result


class TestSummarizeDiffTruncation:
    def _make_long_file_diff(self, n_lines: int) -> str:
        header = (
            f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1,1 +1,{n_lines} @@\n"
        )
        body = "\n".join(f"+line {i}" for i in range(n_lines))
        return header + body

    def test_truncation_shows_omitted_count(self):
        diff = self._make_long_file_diff(50)
        result = summarize_diff(diff, lines_per_file=10)
        # 4 header lines + 50 content lines = 54 total; 54 - 10 = 44 omitted
        assert "[44 more lines in big.py]" in result

    def test_no_truncation_when_within_limit(self):
        diff = self._make_long_file_diff(5)
        result = summarize_diff(diff, lines_per_file=20)
        assert "more lines" not in result

    def test_custom_lines_per_file(self):
        diff = self._make_long_file_diff(30)
        result = summarize_diff(diff, lines_per_file=5)
        # 4 header lines + 30 content lines = 34 total; 34 - 5 = 29 omitted
        assert "[29 more lines in big.py]" in result


# ---------------------------------------------------------------------------
# progress tests
# ---------------------------------------------------------------------------


class TestProgress:
    def test_prints_message(self, capsys):
        progress("hello world")
        assert capsys.readouterr().out == "hello world\n"

    def test_broken_pipe_does_not_raise(self):
        with patch("builtins.print", side_effect=BrokenPipeError):
            # Must not raise
            progress("ignored")

    def test_broken_pipe_returns_none(self):
        with patch("builtins.print", side_effect=BrokenPipeError):
            result = progress("ignored")
            assert result is None
