"""Markdown adaptation for Discord: tables, deep headings, rules, splitting.

    .venv/bin/python -m unittest tests.test_discord_markdown
"""

import re
import unittest

from harness.discord_markdown import adapt, split_message

WIDE_TWO_COL = """\
Here's the breakdown:

| Problem | Pinches affected |
|---|---|
| Rejected by the palm check (palm 48–56° from gaze, limit 45°) | 73 of 82 |
| Head pointed at the panel, but the Quest hand pointer missed it | 59 of 64 |

That's the summary."""

WIDE_THREE_COL = """\
| Setup | Even spacing | Greedy |
|---|---|---|
| `You_re_Beautiful` setup 1 (25 min) | 80% | 87% |
| `Perfect_10` setup 0 (8 min) | 72% | 78% |"""

NARROW = """\
| Name | Count |
|------|------:|
| **apples** | 3 |
| pears | 12 |"""


class AdaptTests(unittest.TestCase):
    def test_wide_two_column_table_becomes_captioned_list(self):
        out = adapt(WIDE_TWO_COL)
        self.assertNotIn("|", out)
        self.assertIn("_Problem → Pinches affected_", out)
        self.assertIn(
            "- **Rejected by the palm check (palm 48–56° from gaze, limit 45°)**: 73 of 82", out
        )
        self.assertTrue(out.startswith("Here's the breakdown:\n"))
        self.assertTrue(out.endswith("\nThat's the summary."))

    def test_wide_multi_column_table_labels_each_value(self):
        out = adapt(WIDE_THREE_COL)
        self.assertEqual(out.splitlines()[0],
                         "- **`You_re_Beautiful` setup 1 (25 min)** — Even spacing: 80% · Greedy: 87%")

    def test_narrow_table_becomes_aligned_code_block(self):
        lines = adapt(NARROW).splitlines()
        self.assertEqual(lines[0], "```")
        self.assertEqual(lines[-1], "```")
        self.assertEqual(lines[1], "Name     Count")
        self.assertEqual(lines[3], "apples       3")  # bold stripped, right-aligned number
        self.assertEqual(lines[4], "pears       12")

    def test_already_bold_first_cell_not_double_bolded(self):
        out = adapt("| A | B |\n|---|---|\n| **" + "x" * 60 + "** | y |")
        self.assertIn("- **" + "x" * 60 + "**: y", out)
        self.assertNotIn("****", out)

    def test_escaped_pipe_stays_in_cell(self):
        out = adapt("| Expr | Meaning that is fairly long to force list form |\n|---|---|\n| a \\| b | either |")
        self.assertIn("- **a | b**: either", out)

    def test_code_blocks_untouched(self):
        text = "```\n| a | b |\n|---|---|\n#### keep\n---\n```"
        self.assertEqual(adapt(text), text)

    def test_deep_headings_and_rules(self):
        self.assertEqual(adapt("### ok\n#### Deep one\n---\ntext"), "### ok\n**Deep one**\ntext")

    def test_pipes_in_prose_are_not_tables(self):
        text = "Use a | b in shell.\nNext line | here"
        self.assertEqual(adapt(text), text)


class SplitTests(unittest.TestCase):
    def check(self, chunks, limit):
        for c in chunks:
            self.assertLessEqual(len(c), limit)
            self.assertEqual(len(re.findall(r"^```", c, re.M)) % 2, 0, c)  # fences balanced

    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_message("hi", 2000), ["hi"])

    def test_splits_at_line_boundaries(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
        chunks = split_message(text, 2000)
        self.check(chunks, 2000)
        self.assertEqual("\n".join(chunks), text)
        self.assertTrue(all(c.splitlines()[-1].startswith("line") for c in chunks))

    def test_code_block_closed_and_reopened(self):
        code = "\n".join(f"print({i})  # " + "y" * 40 for i in range(80))
        text = "Intro\n```python\n" + code + "\n```\nOutro"
        chunks = split_message(text, 1000)
        self.check(chunks, 1000)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(chunks[1].startswith("```python\n"))
        joined = "".join(chunks)
        for i in range(80):
            self.assertIn(f"print({i})", joined)
        self.assertTrue(chunks[-1].endswith("Outro"))

    def test_overlong_line_is_cut(self):
        text = "a" * 5000
        chunks = split_message(text, 2000)
        self.check(chunks, 2000)
        self.assertEqual("".join(chunks), text)

    def test_overlong_line_inside_code_block(self):
        text = "```\n" + "z" * 4500 + "\n```"
        chunks = split_message(text, 2000)
        self.check(chunks, 2000)
        self.assertEqual("".join(chunks).count("z"), 4500)


if __name__ == "__main__":
    unittest.main()
