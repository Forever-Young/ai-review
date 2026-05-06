import json
import configparser
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from ai_review.cli import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_FILE,
    DEFAULT_PUBLISH_PENDING,
    build_config,
    changed_right_lines,
    main,
    parse_pr_url,
    parse_review,
    validate_comments,
)


class CliTests(unittest.TestCase):
    def test_parse_pr_url(self):
        ref = parse_pr_url("https://github.com/example/project/pull/42/files")
        self.assertEqual(ref.owner, "example")
        self.assertEqual(ref.repo, "project")
        self.assertEqual(ref.number, 42)

    def test_changed_right_lines_tracks_added_lines(self):
        diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 keep
-old
+new
+added
 context
"""
        self.assertEqual(changed_right_lines(diff), {"app.py": {2, 3}})

    def test_validate_comments_rejects_non_changed_lines(self):
        review = parse_review(
            json.dumps(
                {
                    "summary": "summary",
                    "comments": [
                        {"path": "app.py", "line": 2, "body": "valid"},
                        {"path": "app.py", "line": 9, "body": "invalid"},
                    ],
                }
            )
        )

        validated = validate_comments(review, {"app.py": {2}}, max_comments=10)

        self.assertEqual([comment.line for comment in validated.comments], [2])

    def test_config_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.cache_dir, DEFAULT_CACHE_DIR)
        self.assertEqual(config.config_file, config_path)
        self.assertEqual(config.ai_provider, "codex")
        self.assertIsNone(config.codex_model)
        self.assertEqual(config.publish_pending, DEFAULT_PUBLISH_PENDING)
        self.assertEqual(config.max_comments, 20)

    def test_missing_config_file_is_created_with_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".config" / "ai-review" / "ai-review.conf"

            build_config(self.args(config_file=config_path))

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(parser.get("git", "cache_dir"), str(DEFAULT_CACHE_DIR))
        self.assertEqual(parser.get("ai", "provider"), "codex")
        self.assertEqual(parser.get("codex", "model"), "")
        self.assertEqual(parser.get("review", "max_comments"), "20")
        self.assertEqual(parser.get("github", "publish_pending"), "yes")

    def test_missing_config_file_uses_cli_values_when_creating_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"

            config = build_config(
                self.args(
                    config_file=config_path,
                    cache_dir="/tmp/review-cache",
                    ai_provider="codex",
                    codex_model="gpt-5.2",
                    publish_pending=True,
                    max_comments=11,
                    save_prompt="/tmp/prompt.txt",
                )
            )
            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(config.cache_dir, Path("/tmp/review-cache"))
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertTrue(config.publish_pending)
        self.assertEqual(config.max_comments, 11)
        self.assertEqual(parser.get("git", "cache_dir"), "/tmp/review-cache")
        self.assertEqual(parser.get("codex", "model"), "gpt-5.2")
        self.assertEqual(parser.get("review", "max_comments"), "11")
        self.assertEqual(parser.get("review", "save_prompt"), "/tmp/prompt.txt")
        self.assertEqual(parser.get("github", "publish_pending"), "yes")

    def test_config_file_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
cache_dir = ~/custom-ai-review-cache

[ai]
provider = codex

[codex]
model = gpt-5.2

[github]
publish_pending = yes

[review]
max_comments = 7
save_prompt = ~/review-prompt.txt
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.cache_dir, Path("~/custom-ai-review-cache").expanduser())
        self.assertEqual(config.ai_provider, "codex")
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertTrue(config.publish_pending)
        self.assertEqual(config.max_comments, 7)
        self.assertEqual(config.save_prompt, Path("~/review-prompt.txt").expanduser())

    def test_cli_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
cache_dir = /from/config

[ai]
provider = codex

[github]
publish_pending = yes
""".strip(),
                encoding="utf-8",
            )

            config = build_config(
                self.args(
                    config_file=config_path,
                    cache_dir="/from/cli",
                    ai_provider="codex",
                    codex_model="gpt-5.2",
                    publish_pending=False,
                    max_comments=3,
                )
            )

        self.assertEqual(config.cache_dir, Path("/from/cli"))
        self.assertEqual(config.ai_provider, "codex")
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertFalse(config.publish_pending)
        self.assertEqual(config.max_comments, 3)

    def test_rejects_unknown_ai_provider(self):
        with self.assertRaises(SystemExit):
            with tempfile.TemporaryDirectory() as temp_dir:
                build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="command"))

    def test_ai_provider_is_codex_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="codex"))

        self.assertEqual(config.ai_provider, "codex")

    def test_main_without_pr_url_still_creates_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"

            with self.assertRaises(SystemExit):
                main(["--config-file", str(config_path)])

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(parser.get("ai", "provider"), "codex")
        self.assertEqual(parser.get("codex", "model"), "")
        self.assertEqual(parser.get("git", "cache_dir"), str(DEFAULT_CACHE_DIR))
        self.assertEqual(parser.get("github", "publish_pending"), "yes")

    def args(self, **overrides):
        values = {
            "config_file": DEFAULT_CONFIG_FILE,
            "cache_dir": None,
            "ai_provider": None,
            "codex_model": None,
            "publish_pending": None,
            "max_comments": None,
            "save_prompt": None,
        }
        values.update(overrides)
        return Namespace(**values)


if __name__ == "__main__":
    unittest.main()
