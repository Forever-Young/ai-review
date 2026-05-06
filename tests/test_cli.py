import json
import configparser
import tempfile
import unittest
from argparse import Namespace
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import call, patch

from ai_review import jira as jira_client
from ai_review.cli import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_FILE,
    DEFAULT_PUBLISH_PENDING,
    DEFAULT_SMART_REVIEW,
    PullRequest,
    PullRequestRef,
    build_config,
    build_prompt,
    build_review_body,
    changed_right_lines,
    create_or_replace_pending_review,
    create_pending_review,
    extract_ticket_key_heuristic,
    fetch_jira_tool_context,
    gh_api,
    git_ssh_command_from_key,
    ensure_checkout,
    is_auth_error,
    load_pr_discussion_context,
    main,
    parse_pr_url,
    parse_review,
    run_codex,
    run_cursor,
    run_gemini,
    run_ai_provider,
    run_review_for_bot_result,
    run,
    validate_comments,
)


class CliTests(unittest.TestCase):
    def test_parse_pr_url(self):
        ref = parse_pr_url("https://github.com/example/project/pull/42/files")
        self.assertEqual(ref.owner, "example")
        self.assertEqual(ref.repo, "project")
        self.assertEqual(ref.number, 42)

    def test_extract_ticket_key_from_branch(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="ABC12-123-something-short-desc",
        )

        self.assertEqual(extract_ticket_key_heuristic(pr, ""), "ABC12-123")

    def test_extract_ticket_key_from_first_comment_refs(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="feat: add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        self.assertEqual(extract_ticket_key_heuristic(pr, "Refs: ABC-123"), "ABC-123")

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

    def test_parse_review_supports_pr_level_comments(self):
        review = parse_review(
            json.dumps(
                {
                    "summary": "summary",
                    "pr_comments": ["Missing the requested empty-state behavior."],
                    "comments": [],
                }
            )
        )

        self.assertEqual(review.pr_comments, ["Missing the requested empty-state behavior."])
        self.assertIn("PR-level comments", build_review_body(review))

    def test_auth_error_detection(self):
        self.assertTrue(is_auth_error("HTTP 401 unauthorized"))
        self.assertTrue(is_auth_error("OAuth token expired"))
        self.assertFalse(is_auth_error("tool not found"))

    def test_jira_context_authenticates_and_retries_on_auth_failure(self):
        calls = [SystemExit("HTTP 401 unauthorized"), '{"ok": true}']

        with patch("ai_review.cli.mcporter_call", side_effect=calls) as call_mock:
            with patch("ai_review.cli.mcporter_auth") as auth_mock:
                context = fetch_jira_tool_context("Issue", "jira.get_issue", "issueKey", "ABC-123", 60000)

        self.assertIn('{"ok": true}', context)
        self.assertEqual(call_mock.call_count, 2)
        auth_mock.assert_called_once()

    def test_mcporter_call_uses_jira_remote_fallback_when_server_missing(self):
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs):
            commands.append(cmd)
            return '{"ok": true}'

        output = jira_client.mcporter_call(
            "jira.get_issue",
            {"issueKey": "ABC-123"},
            60000,
            run_cmd=fake_run,
            status_cb=lambda _msg, _detail=None: None,
        )

        self.assertIn('"ok": true', output)
        self.assertEqual(commands[0][0:2], ["mcporter", "call"])
        self.assertIn("--stdio", commands[0])
        self.assertIn("npx", commands[0])
        self.assertIn("mcp-remote@latest", commands[0])
        self.assertIn("https://mcp.atlassian.com/v1/mcp", commands[0])

    def test_mcporter_call_raises_when_tool_returns_is_error_payload(self):
        def fake_run(_cmd: list[str], **_kwargs):
            return "{ content: [ { type: 'text', text: 'MCP error -32602: Tool get_issue not found' } ], isError: true }"

        with self.assertRaises(SystemExit) as caught:
            jira_client.mcporter_call(
                "jira.get_issue",
                {"issueKey": "ABC-123"},
                60000,
                run_cmd=fake_run,
                status_cb=lambda _msg, _detail=None: None,
            )

        self.assertIn("Tool get_issue not found", str(caught.exception))

    def test_fetch_jira_tool_context_adds_cloud_id_for_get_jira_issue(self):
        auth_var = ContextVar("auth_var", default=None)
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_call_tool(selector: str, args: dict[str, object], _timeout: int) -> str:
            calls.append((selector, args))
            if selector == "jira.getJiraIssue":
                return json.dumps({"fields": {"summary": "Issue summary"}})
            raise AssertionError(f"Unexpected selector {selector}")

        with patch("ai_review.jira.jira_cloud_id", return_value="cloud-1"):
            context = jira_client.fetch_jira_tool_context(
                "Issue",
                "jira.getJiraIssue",
                "issueIdOrKey",
                "ABC-123",
                60000,
                run_cmd=lambda *_args, **_kwargs: "",
                status_cb=lambda _msg, _detail=None: None,
                auth_handler_var=auth_var,
                call_tool=fake_call_tool,
                auth_func=lambda _server, _timeout: None,
            )

        self.assertIn("Issue summary", context)
        self.assertEqual(calls[0][1]["cloudId"], "cloud-1")
        self.assertEqual(calls[0][1]["issueIdOrKey"], "ABC-123")

    def test_jira_context_raises_clear_error_when_jira_mcp_unavailable(self):
        auth_var = ContextVar("auth_var", default=None)

        with self.assertRaises(SystemExit) as caught:
            jira_client.fetch_jira_tool_context(
                "Issue",
                "jira.get_issue",
                "issueKey",
                "ABC-123",
                60000,
                run_cmd=lambda *_args, **_kwargs: "",
                status_cb=lambda _msg, _detail=None: None,
                auth_handler_var=auth_var,
                call_tool=lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("Unknown MCP server 'jira'.")),
                auth_func=lambda _server, _timeout: None,
            )

        self.assertIn("Jira MCP is unavailable", str(caught.exception))

    def test_jira_context_logs_issue_title_when_available(self):
        auth_var = ContextVar("auth_var", default=None)
        statuses: list[tuple[str, str | None]] = []

        context = jira_client.fetch_jira_tool_context(
            "Issue",
            "jira.get_issue",
            "issueKey",
            "ABC-123",
            60000,
            run_cmd=lambda *_args, **_kwargs: "",
            status_cb=lambda message, detail=None: statuses.append((message, detail)),
            auth_handler_var=auth_var,
            call_tool=lambda *_args, **_kwargs: json.dumps({"fields": {"summary": "Fix login redirect loop"}}),
            auth_func=lambda _server, _timeout: None,
        )

        self.assertIn("Fix login redirect loop", context)
        self.assertIn(("Loaded Jira issue title", "Fix login redirect loop"), statuses)

    def test_jira_issue_title_from_non_json_raw(self):
        raw = "{ content: [ { text: 'ok' } ], issue: { summary: 'Backfill records to avoid duplicates' } }"
        self.assertEqual(jira_client.jira_issue_title_from_raw(raw), "Backfill records to avoid duplicates")

    def test_config_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.cache_dir, DEFAULT_CACHE_DIR)
        self.assertIsNone(config.git_ssh_command)
        self.assertEqual(config.config_file, config_path)
        self.assertEqual(config.ai_provider, "codex")
        self.assertIsNone(config.codex_model)
        self.assertIsNone(config.cursor_model)
        self.assertIsNone(config.gemini_model)
        self.assertIsNone(config.claude_model)
        self.assertIsNone(config.github_token)
        self.assertEqual(config.publish_pending, DEFAULT_PUBLISH_PENDING)
        self.assertEqual(config.smart_review, DEFAULT_SMART_REVIEW)
        self.assertEqual(config.max_comments, 20)
        self.assertIsNone(config.telegram.bot_token)
        self.assertIsNone(config.telegram.authorized_user_id)
        self.assertFalse(config.telegram.jira_auth_via_telegram)

    def test_missing_config_file_is_created_with_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".config" / "ai-review" / "ai-review.conf"

            build_config(self.args(config_file=config_path))

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(parser.get("git", "cache_dir"), str(DEFAULT_CACHE_DIR))
        self.assertEqual(parser.get("git", "ssh_key_path"), "")
        self.assertEqual(parser.get("git", "ssh_command"), "")
        self.assertEqual(parser.get("ai", "provider"), "codex")
        self.assertEqual(parser.get("codex", "model"), "")
        self.assertEqual(parser.get("cursor", "model"), "")
        self.assertEqual(parser.get("gemini", "model"), "")
        self.assertEqual(parser.get("claude", "model"), "")
        self.assertEqual(parser.get("jira", "enabled"), "no")
        self.assertEqual(parser.get("telegram", "bot_token"), "")
        self.assertEqual(parser.get("telegram", "authorized_user_id"), "")
        self.assertEqual(parser.get("telegram", "jira_auth_via_telegram"), "no")
        self.assertEqual(parser.get("review", "max_comments"), "20")
        self.assertEqual(parser.get("review", "smart_review"), "no")
        self.assertEqual(parser.get("github", "publish_pending"), "yes")
        self.assertEqual(parser.get("github", "token"), "")

    def test_missing_config_file_uses_cli_values_when_creating_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"

            config = build_config(
                self.args(
                    config_file=config_path,
                    cache_dir="/tmp/review-cache",
                    ai_provider="codex",
                    codex_model="gpt-5.2",
                    cursor_model="cursor-model",
                    gemini_model="gemini-2.5-pro",
                    claude_model="sonnet",
                    publish_pending=True,
                    smart_review=True,
                    max_comments=11,
                    save_prompt="/tmp/prompt.txt",
                )
            )
            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(config.cache_dir, Path("/tmp/review-cache"))
        self.assertIsNone(config.git_ssh_command)
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertEqual(config.cursor_model, "cursor-model")
        self.assertEqual(config.gemini_model, "gemini-2.5-pro")
        self.assertEqual(config.claude_model, "sonnet")
        self.assertTrue(config.publish_pending)
        self.assertTrue(config.smart_review)
        self.assertEqual(config.max_comments, 11)
        self.assertEqual(parser.get("git", "cache_dir"), "/tmp/review-cache")
        self.assertEqual(parser.get("git", "ssh_key_path"), "")
        self.assertEqual(parser.get("git", "ssh_command"), "")
        self.assertEqual(parser.get("codex", "model"), "gpt-5.2")
        self.assertEqual(parser.get("cursor", "model"), "cursor-model")
        self.assertEqual(parser.get("gemini", "model"), "gemini-2.5-pro")
        self.assertEqual(parser.get("claude", "model"), "sonnet")
        self.assertEqual(parser.get("jira", "enabled"), "no")
        self.assertEqual(parser.get("telegram", "bot_token"), "")
        self.assertEqual(parser.get("telegram", "authorized_user_id"), "")
        self.assertEqual(parser.get("telegram", "jira_auth_via_telegram"), "no")
        self.assertEqual(parser.get("review", "max_comments"), "11")
        self.assertEqual(parser.get("review", "smart_review"), "yes")
        self.assertEqual(parser.get("review", "save_prompt"), "/tmp/prompt.txt")
        self.assertEqual(parser.get("github", "publish_pending"), "yes")

    def test_config_file_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
cache_dir = ~/custom-ai-review-cache
ssh_command = ssh -i ~/.ssh/custom_key -o IdentitiesOnly=yes -o IdentityAgent=none

[ai]
provider = codex

[codex]
model = gpt-5.2

[github]
publish_pending = yes
token = ghp_example

[review]
max_comments = 7
smart_review = yes
save_prompt = ~/review-prompt.txt

[telegram]
bot_token = "123:abc"
authorized_user_id = 987
jira_auth_via_telegram = yes
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.cache_dir, Path("~/custom-ai-review-cache").expanduser())
        self.assertEqual(config.git_ssh_command, "ssh -i ~/.ssh/custom_key -o IdentitiesOnly=yes -o IdentityAgent=none")
        self.assertEqual(config.ai_provider, "codex")
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertEqual(config.github_token, "ghp_example")
        self.assertTrue(config.publish_pending)
        self.assertTrue(config.smart_review)
        self.assertEqual(config.max_comments, 7)
        self.assertEqual(config.save_prompt, Path("~/review-prompt.txt").expanduser())
        self.assertEqual(config.telegram.bot_token, "123:abc")
        self.assertEqual(config.telegram.authorized_user_id, 987)
        self.assertTrue(config.telegram.jira_auth_via_telegram)

    def test_existing_config_file_is_backfilled_with_missing_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
cache_dir = /custom-cache

[github]
publish_pending = no
key = ghp_alias
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(config.cache_dir, Path("/custom-cache"))
        self.assertFalse(config.publish_pending)
        self.assertEqual(config.github_token, "ghp_alias")
        self.assertEqual(parser.get("git", "cache_dir"), "/custom-cache")
        self.assertEqual(parser.get("git", "ssh_key_path"), "")
        self.assertEqual(parser.get("git", "ssh_command"), "")
        self.assertEqual(parser.get("ai", "provider"), "codex")
        self.assertEqual(parser.get("codex", "model"), "")
        self.assertEqual(parser.get("cursor", "model"), "")
        self.assertEqual(parser.get("gemini", "model"), "")
        self.assertEqual(parser.get("claude", "model"), "")
        self.assertEqual(parser.get("review", "max_comments"), "20")
        self.assertEqual(parser.get("review", "smart_review"), "no")
        self.assertEqual(parser.get("github", "publish_pending"), "no")
        self.assertEqual(parser.get("github", "key"), "ghp_alias")
        self.assertEqual(parser.get("github", "token"), "")
        self.assertEqual(parser.get("jira", "enabled"), "no")
        self.assertEqual(parser.get("telegram", "bot_token"), "")
        self.assertEqual(parser.get("telegram", "authorized_user_id"), "")
        self.assertEqual(parser.get("telegram", "jira_auth_via_telegram"), "no")

    def test_config_rejects_invalid_telegram_authorized_user_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[telegram]
authorized_user_id = not-a-number
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                build_config(self.args(config_file=config_path))

    def test_config_file_accepts_github_key_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[github]
key = ghp_alias
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.github_token, "ghp_alias")

    def test_config_file_strips_quotes_from_github_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[github]
token = "ghp_quoted"
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(config.github_token, "ghp_quoted")

    def test_config_file_builds_git_ssh_command_from_key_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            key_path = Path(temp_dir) / "key with spaces"
            config_path.write_text(
                f"""
[git]
ssh_key_path = {key_path}
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(
            config.git_ssh_command,
            f"ssh -i '{key_path}' -o IdentitiesOnly=yes -o IdentityAgent=none",
        )

    def test_config_file_expands_home_in_git_ssh_key_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
ssh_key_path = ~/.ssh/review_key
""".strip(),
                encoding="utf-8",
            )

            config = build_config(self.args(config_file=config_path))

        self.assertEqual(
            config.git_ssh_command,
            f"ssh -i {Path('~/.ssh/review_key').expanduser()} -o IdentitiesOnly=yes -o IdentityAgent=none",
        )

    def test_config_rejects_git_ssh_key_path_and_command_together(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[git]
ssh_key_path = ~/.ssh/review_key
ssh_command = ssh -i ~/.ssh/other_key
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                build_config(self.args(config_file=config_path))

    def test_git_ssh_command_from_key_bypasses_agent(self):
        command = git_ssh_command_from_key(Path("/tmp/review key"))

        self.assertEqual(command, "ssh -i '/tmp/review key' -o IdentitiesOnly=yes -o IdentityAgent=none")

    def test_ensure_checkout_uses_ssh_url_and_command_for_network_git(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("ai_review.cli.run", return_value="") as run_mock:
                checkout = ensure_checkout(pr, Path(temp_dir), "ssh -i /tmp/key -o IdentitiesOnly=yes")

        self.assertEqual(checkout, Path(temp_dir) / "worktrees" / "example__project__pr-42")
        first_call = run_mock.call_args_list[0]
        self.assertEqual(
            first_call,
            call(
                ["git", "clone", "--mirror", "git@github.com:example/project.git", str(Path(temp_dir) / "repos" / "example__project.git")],
                env=first_call.kwargs["env"],
            ),
        )
        self.assertEqual(first_call.kwargs["env"]["GIT_SSH_COMMAND"], "ssh -i /tmp/key -o IdentitiesOnly=yes")

    def test_ensure_checkout_uses_https_without_ssh_command(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("ai_review.cli.run", return_value="") as run_mock:
                ensure_checkout(pr, Path(temp_dir))

        first_call = run_mock.call_args_list[0]
        self.assertEqual(first_call.args[0], ["git", "clone", "--mirror", "https://github.com/example/project.git", str(Path(temp_dir) / "repos" / "example__project.git")])
        self.assertIsNone(first_call.kwargs["env"])

    def test_gh_api_uses_configured_token_environment(self):
        with patch("ai_review.cli.run", return_value="{}") as run_mock:
            gh_api("repos/example/project/pulls/42", github_token="ghp_example")

        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["GH_TOKEN"], "ghp_example")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")

    def test_gh_api_does_not_pass_env_without_configured_token(self):
        with patch("ai_review.cli.run", return_value="{}") as run_mock:
            gh_api("repos/example/project/pulls/42", github_token=None)

        self.assertIsNone(run_mock.call_args.kwargs["env"])

    def test_create_or_replace_pending_review_replaces_current_user_pending_review(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )
        review = parse_review(json.dumps({"summary": "summary", "comments": []}))

        with patch(
            "ai_review.cli.gh_api",
            side_effect=[
                '{"login": "me"}',
                json.dumps(
                    [
                        {"id": 80, "state": "PENDING", "user": {"login": "other"}},
                        {"id": 81, "state": "PENDING", "user": {"login": "me"}},
                    ]
                ),
                '{"id": 81}',
                '{"id": 82, "html_url": "https://github.com/example/project/pull/42#pullrequestreview-82"}',
            ],
        ) as gh_mock:
            result = create_or_replace_pending_review(pr, review, github_token="ghp_example")

        self.assertTrue(result.replaced_existing)
        self.assertEqual(result.review_id, 82)
        self.assertEqual(result.html_url, "https://github.com/example/project/pull/42#pullrequestreview-82")
        self.assertEqual(gh_mock.call_args_list[0].args[0], "user")
        self.assertEqual(gh_mock.call_args_list[1].args[0], "repos/example/project/pulls/42/reviews?per_page=100&page=1")
        self.assertEqual(gh_mock.call_args_list[2].args[0], "repos/example/project/pulls/42/reviews/81")
        self.assertEqual(gh_mock.call_args_list[2].kwargs["method"], "DELETE")
        self.assertEqual(gh_mock.call_args_list[3].args[0], "repos/example/project/pulls/42/reviews")

    def test_create_or_replace_pending_review_creates_when_no_current_user_pending_review(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )
        review = parse_review(json.dumps({"summary": "summary", "comments": []}))

        with patch(
            "ai_review.cli.gh_api",
            side_effect=[
                '{"login": "me"}',
                json.dumps([{"id": 80, "state": "PENDING", "user": {"login": "other"}}]),
                '{"id": 82, "html_url": "https://github.com/example/project/pull/42#pullrequestreview-82"}',
            ],
        ) as gh_mock:
            result = create_or_replace_pending_review(pr, review, github_token="ghp_example")

        self.assertFalse(result.replaced_existing)
        self.assertEqual(result.review_id, 82)
        self.assertEqual(result.html_url, "https://github.com/example/project/pull/42#pullrequestreview-82")
        self.assertEqual(len(gh_mock.call_args_list), 3)
        self.assertEqual(gh_mock.call_args_list[2].args[0], "repos/example/project/pulls/42/reviews")

    def test_create_pending_review_builds_fallback_url(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Add feature",
            body="",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )
        review = parse_review(json.dumps({"summary": "summary", "comments": []}))

        with patch("ai_review.cli.gh_api", return_value='{"id": 82}'):
            review_id, html_url = create_pending_review(pr, review, github_token="ghp_example")

        self.assertEqual(review_id, 82)
        self.assertEqual(html_url, "https://github.com/example/project/pull/42#pullrequestreview-82")

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

[review]
smart_review = yes
""".strip(),
                encoding="utf-8",
            )

            config = build_config(
                self.args(
                    config_file=config_path,
                    cache_dir="/from/cli",
                    ai_provider="codex",
                    codex_model="gpt-5.2",
                    cursor_model="cursor-model",
                    gemini_model="gemini-2.5-pro",
                    claude_model="sonnet",
                    publish_pending=False,
                    smart_review=False,
                    max_comments=3,
                )
            )

        self.assertEqual(config.cache_dir, Path("/from/cli"))
        self.assertEqual(config.ai_provider, "codex")
        self.assertEqual(config.codex_model, "gpt-5.2")
        self.assertEqual(config.cursor_model, "cursor-model")
        self.assertEqual(config.gemini_model, "gemini-2.5-pro")
        self.assertEqual(config.claude_model, "sonnet")
        self.assertFalse(config.publish_pending)
        self.assertFalse(config.smart_review)
        self.assertEqual(config.max_comments, 3)

    def test_load_pr_discussion_context_includes_issue_reviews_and_inline_comments(self):
        ref = PullRequestRef("example", "project", 42)

        with patch(
            "ai_review.cli.gh_api",
            side_effect=[
                json.dumps(
                    [
                        {
                            "body": "author follow-up",
                            "user": {"login": "author"},
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
                json.dumps(
                    [
                        {
                            "body": "request changes summary",
                            "state": "CHANGES_REQUESTED",
                            "user": {"login": "reviewer"},
                            "submitted_at": "2026-01-02T00:00:00Z",
                        }
                    ]
                ),
                json.dumps(
                    [
                        {
                            "body": "inline concern",
                            "path": "app.py",
                            "line": 12,
                            "user": {"login": "reviewer"},
                            "created_at": "2026-01-02T00:01:00Z",
                        }
                    ]
                ),
            ],
        ):
            context = load_pr_discussion_context(ref)

        self.assertIn("author follow-up", context)
        self.assertIn("request changes summary", context)
        self.assertIn("inline concern", context)
        self.assertIn("app.py:12", context)

    def test_build_prompt_includes_pr_discussion_and_non_smart_rule(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Fix checkout",
            body="PR body",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        prompt = build_prompt(pr, "diff --git a/app.py b/app.py", None, "existing review context", False)

        self.assertIn("Smart review is disabled", prompt)
        self.assertIn("existing review context", prompt)
        self.assertIn("Strictly use only the PR metadata", prompt)

    def test_build_prompt_allows_file_context_in_smart_mode(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Fix checkout",
            body="PR body",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        prompt = build_prompt(pr, "diff --git a/app.py b/app.py", None, "existing review context", True)

        self.assertIn("Smart review is enabled", prompt)
        self.assertIn("Inspect checked-out project files", prompt)
        self.assertIn("smallest directly related files", prompt)
        self.assertIn("targeted fallback", prompt)
        self.assertIn("Do not scan the whole project", prompt)

    def test_run_codex_disables_approval_prompts_and_uses_empty_workspace_without_cwd(self):
        with patch("ai_review.cli.run", return_value='{"summary": "ok"}') as run_mock:
            raw = run_codex("prompt", None, "gpt-5.2")

        self.assertEqual(raw, '{"summary": "ok"}')
        cmd = run_mock.call_args.args[0]
        self.assertIn("--config", cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        cwd = Path(cmd[cmd.index("-C") + 1])
        self.assertEqual(cwd.name, "empty-workspace")

    def test_run_cursor_uses_empty_workspace_without_cwd(self):
        with patch("ai_review.cli.run", return_value='{"summary": "ok"}') as run_mock:
            raw = run_cursor("prompt", None)

        self.assertEqual(raw, '{"summary": "ok"}')
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd, ["cursor-agent", "--print", "--output-format", "text", "--trust", "prompt"])
        cwd = run_mock.call_args.kwargs["cwd"]
        self.assertEqual(cwd.name, "empty-workspace")

    def test_run_gemini_uses_empty_workspace_without_cwd(self):
        with patch("ai_review.cli.run", return_value='{"summary": "ok"}') as run_mock:
            raw = run_gemini("prompt", None, "gemini-2.5-pro")

        self.assertEqual(raw, '{"summary": "ok"}')
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd, ["gemini", "--model", "gemini-2.5-pro"])
        cwd = run_mock.call_args.kwargs["cwd"]
        self.assertEqual(cwd.name, "empty-workspace")
        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["GEMINI_CLI_TRUST_WORKSPACE"], "true")

    def test_run_ai_provider_dispatches_claude(self):
        with patch("ai_review.cli.claude_provider.run_claude", return_value='{"summary":"ok"}') as claude_mock:
            raw = run_ai_provider("prompt", None, "claude", "sonnet")

        self.assertEqual(raw, '{"summary":"ok"}')
        claude_mock.assert_called_once()

    def test_run_mcporter_cleans_up_process_group(self):
        process = type("Proc", (), {})()
        process.pid = 43210
        process.returncode = 0
        process.communicate = lambda input=None: ('{"ok":true}', "")

        with patch("ai_review.cli.subprocess.Popen", return_value=process) as popen_mock, patch(
            "ai_review.cli.os.killpg"
        ) as killpg_mock:
            output = run(["mcporter", "call", "jira.getJiraIssue"])

        self.assertEqual(output, '{"ok":true}')
        self.assertTrue(popen_mock.called)
        killpg_mock.assert_called_once()

    def test_run_non_mcporter_does_not_call_killpg(self):
        with patch("ai_review.cli.subprocess.run") as run_mock, patch("ai_review.cli.os.killpg") as killpg_mock:
            run_mock.return_value = type("Done", (), {"stdout": "ok"})()
            output = run(["git", "--version"])

        self.assertEqual(output, "ok")
        killpg_mock.assert_not_called()

    def test_main_skips_checkout_when_smart_review_is_disabled(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Fix checkout",
            body="PR body",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("ai_review.cli.load_pr", return_value=pr), patch(
                "ai_review.cli.load_pr_discussion_context", return_value="No comments."
            ), patch("ai_review.cli.load_jira_context", return_value=None), patch(
                "ai_review.cli.load_diff", return_value="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            ), patch("ai_review.cli.ensure_checkout") as checkout_mock, patch(
                "ai_review.cli.run_codex", return_value='{"summary": "ok", "comments": []}'
            ), patch(
                "ai_review.cli.create_or_replace_pending_review"
            ):
                main(
                    [
                        "--config-file",
                        str(Path(temp_dir) / "ai-review.conf"),
                        "--no-publish-pending",
                        "https://github.com/example/project/pull/42",
                    ]
                )

        checkout_mock.assert_not_called()

    def test_main_checks_out_when_smart_review_is_enabled(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Fix checkout",
            body="PR body",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir) / "checkout"
            with patch("ai_review.cli.load_pr", return_value=pr), patch(
                "ai_review.cli.load_pr_discussion_context", return_value="No comments."
            ), patch("ai_review.cli.load_jira_context", return_value=None), patch(
                "ai_review.cli.load_diff", return_value="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            ), patch("ai_review.cli.ensure_checkout", return_value=checkout) as checkout_mock, patch(
                "ai_review.cli.run_codex", return_value='{"summary": "ok", "comments": []}'
            ), patch(
                "ai_review.cli.create_or_replace_pending_review"
            ):
                main(
                    [
                        "--config-file",
                        str(Path(temp_dir) / "ai-review.conf"),
                        "--no-publish-pending",
                        "--smart-review",
                        "https://github.com/example/project/pull/42",
                    ]
                )

        checkout_mock.assert_called_once()

    def test_bot_review_returns_only_pending_review_url_on_success(self):
        pr = PullRequest(
            ref=PullRequestRef("example", "project", 42),
            title="Fix checkout",
            body="PR body",
            html_url="https://github.com/example/project/pull/42",
            base_clone_url="https://github.com/example/project.git",
            base_ssh_url="git@github.com:example/project.git",
            base_sha="base",
            head_sha="head",
            head_ref="feature/add-stuff",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf"))
            with patch("ai_review.cli.load_pr", return_value=pr), patch(
                "ai_review.cli.load_pr_discussion_context", return_value="No comments."
            ), patch("ai_review.cli.load_jira_context", return_value=None), patch(
                "ai_review.cli.load_diff", return_value="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            ), patch(
                "ai_review.cli.run_codex", return_value='{"summary": "ok", "comments": []}'
            ), patch(
                "ai_review.cli.create_or_replace_pending_review",
                return_value=type("Result", (), {"html_url": "https://github.com/example/project/pull/42#pullrequestreview-82"})(),
            ):
                ok, output = run_review_for_bot_result("https://github.com/example/project/pull/42", config)

        self.assertTrue(ok)
        self.assertEqual(output, "https://github.com/example/project/pull/42#pullrequestreview-82")

    def test_rejects_unknown_ai_provider(self):
        with self.assertRaises(SystemExit):
            with tempfile.TemporaryDirectory() as temp_dir:
                build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="command"))

    def test_ai_provider_is_codex_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="codex"))

        self.assertEqual(config.ai_provider, "codex")

    def test_ai_provider_is_cursor_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="cursor"))

        self.assertEqual(config.ai_provider, "cursor")

    def test_ai_provider_is_gemini_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="gemini"))

        self.assertEqual(config.ai_provider, "gemini")

    def test_ai_provider_is_claude_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(self.args(config_file=Path(temp_dir) / "ai-review.conf", ai_provider="claude"))

        self.assertEqual(config.ai_provider, "claude")

    def test_main_without_pr_url_still_creates_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"

            with self.assertRaises(SystemExit):
                main(["--config-file", str(config_path)])

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(parser.get("ai", "provider"), "codex")
        self.assertEqual(parser.get("codex", "model"), "")
        self.assertEqual(parser.get("cursor", "model"), "")
        self.assertEqual(parser.get("gemini", "model"), "")
        self.assertEqual(parser.get("claude", "model"), "")
        self.assertEqual(parser.get("git", "cache_dir"), str(DEFAULT_CACHE_DIR))
        self.assertEqual(parser.get("github", "publish_pending"), "yes")
        self.assertEqual(parser.get("review", "smart_review"), "no")
        self.assertEqual(parser.get("telegram", "bot_token"), "")
        self.assertEqual(parser.get("telegram", "authorized_user_id"), "")
        self.assertEqual(parser.get("telegram", "jira_auth_via_telegram"), "no")

    def test_main_without_pr_url_backfills_existing_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"
            config_path.write_text(
                """
[github]
publish_pending = no
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                main(["--config-file", str(config_path)])

            parser = configparser.ConfigParser()
            parser.read(config_path)

        self.assertEqual(parser.get("github", "publish_pending"), "no")
        self.assertEqual(parser.get("git", "cache_dir"), str(DEFAULT_CACHE_DIR))
        self.assertEqual(parser.get("review", "max_comments"), "20")
        self.assertEqual(parser.get("review", "smart_review"), "no")
        self.assertEqual(parser.get("jira", "enabled"), "no")
        self.assertEqual(parser.get("telegram", "bot_token"), "")
        self.assertEqual(parser.get("telegram", "authorized_user_id"), "")
        self.assertEqual(parser.get("telegram", "jira_auth_via_telegram"), "no")

    def test_bot_subcommand_requires_configured_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ai-review.conf"

            with self.assertRaises(SystemExit) as caught:
                main(["--config-file", str(config_path), "bot"])

        self.assertIn("telegram.bot_token", str(caught.exception))

    def args(self, **overrides):
        values = {
            "config_file": DEFAULT_CONFIG_FILE,
            "cache_dir": None,
            "ai_provider": None,
            "codex_model": None,
            "cursor_model": None,
            "gemini_model": None,
            "claude_model": None,
            "publish_pending": None,
            "max_comments": None,
            "save_prompt": None,
            "smart_review": None,
            "jira_enabled": None,
            "jira_auth": False,
        }
        values.update(overrides)
        return Namespace(**values)


if __name__ == "__main__":
    unittest.main()
