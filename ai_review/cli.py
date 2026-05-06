from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import codex as codex_provider
from . import cursor as cursor_provider
from . import gemini as gemini_provider
from . import gh as gh_client
from . import jira as jira_client


GITHUB_PR_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
MAX_DIFF_CHARS = 180_000
DEFAULT_CONFIG_FILE = Path("~/.config/ai-review/ai-review.conf").expanduser()
DEFAULT_CACHE_DIR = Path("~/.local/cache/ai-review").expanduser()
DEFAULT_AI_PROVIDER = "codex"
DEFAULT_PUBLISH_PENDING = True
DEFAULT_SMART_REVIEW = False
DEFAULT_MAX_COMMENTS = 20
SUPPORTED_AI_PROVIDERS = {"codex", "cursor", "gemini"}
DEFAULT_JIRA_ENABLED = False
DEFAULT_JIRA_MCP_SERVER = "jira"
DEFAULT_JIRA_ISSUE_TOOL = "jira.get_issue"
DEFAULT_JIRA_ISSUE_ARG = "issueKey"
DEFAULT_JIRA_COMMENTS_TOOL = "jira.get_comments"
DEFAULT_JIRA_ATTACHMENTS_TOOL = "jira.get_attachments"
DEFAULT_TELEGRAM_AUTHORIZED_USER_ID = ""
DEFAULT_TELEGRAM_BOT_TOKEN = ""
DEFAULT_TELEGRAM_JIRA_AUTH_VIA_TELEGRAM = False
DEFAULT_MCP_TIMEOUT_MS = 60_000
MAX_CONTEXT_CHARS = 60_000
TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
AUTH_ERROR_PATTERNS = (
    "401",
    "403",
    "auth",
    "authorize",
    "authorization",
    "credential",
    "forbidden",
    "login",
    "oauth",
    "permission",
    "token",
    "unauthorized",
)
STATUS_COLOR = "\033[36m"
DETAIL_COLOR = "\033[90m"
RESET_COLOR = "\033[0m"
STATUS_LISTENER: ContextVar[Callable[[str], None] | None] = ContextVar("STATUS_LISTENER", default=None)
JIRA_AUTH_HANDLER: ContextVar[Callable[[], None] | None] = ContextVar("JIRA_AUTH_HANDLER", default=None)


def status(message: str, detail: str | None = None) -> None:
    line = f"{STATUS_COLOR}[ai-review]{RESET_COLOR} {message}"
    if detail:
        line = f"{line} {DETAIL_COLOR}{detail}{RESET_COLOR}"
    print(line, file=sys.stderr)
    listener = STATUS_LISTENER.get()
    if listener:
        plain_line = ANSI_ESCAPE_RE.sub("", line)
        listener(plain_line)


def sanitize_telegram_progress_line(line: str) -> str:
    return re.sub(r"^\[ai-review\]\s*", "", line).strip()


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}"


@dataclass(frozen=True)
class PullRequest:
    ref: PullRequestRef
    title: str
    body: str
    html_url: str
    base_clone_url: str
    base_ssh_url: str
    base_sha: str
    head_sha: str
    head_ref: str


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    comments: list[ReviewComment]
    pr_comments: list[str]


@dataclass(frozen=True)
class PendingReviewResult:
    review_id: int
    html_url: str
    replaced_existing: bool


@dataclass(frozen=True)
class JiraConfig:
    enabled: bool


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str | None
    authorized_user_id: int | None
    jira_auth_via_telegram: bool


@dataclass(frozen=True)
class AppConfig:
    config_file: Path
    cache_dir: Path
    git_ssh_command: str | None
    ai_provider: str
    codex_model: str | None
    cursor_model: str | None
    gemini_model: str | None
    github_token: str | None
    publish_pending: bool
    smart_review: bool
    max_comments: int
    save_prompt: Path | None
    jira: JiraConfig
    telegram: TelegramConfig


def run(
    cmd: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing required executable: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        stdout = exc.stdout.strip()
        detail = stderr or stdout or f"exit code {exc.returncode}"
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{detail}") from exc
    return completed.stdout


def run_streaming(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing required executable: {cmd[0]}") from exc

    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if not line:
            continue
        output_lines.append(line)
        listener = STATUS_LISTENER.get()
        if listener:
            listener(line)
    return_code = process.wait()
    output = "\n".join(output_lines)
    if return_code != 0:
        detail = output.strip() or f"exit code {return_code}"
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{detail}")
    return output


def parse_pr_url(url: str) -> PullRequestRef:
    match = GITHUB_PR_RE.match(url.strip())
    if not match:
        raise SystemExit("Expected a GitHub PR URL like https://github.com/OWNER/REPO/pull/123")
    owner, repo, number = match.groups()
    return PullRequestRef(owner=owner, repo=repo, number=int(number))


def expand_path(value: str) -> Path:
    return Path(value).expanduser()


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise SystemExit(f"Invalid boolean value in config: {value!r}")


def read_config_file(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    return parser


def config_get(parser: configparser.ConfigParser, section: str, key: str) -> str | None:
    if parser.has_option(section, key):
        return parser.get(section, key)
    return None


def blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def config_secret_value(value: str | None) -> str | None:
    value = blank_to_none(value)
    if value is None:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value or None


def bool_config_value(value: bool) -> str:
    return "yes" if value else "no"


def default_config_values(
    args: argparse.Namespace | None = None,
    *,
    use_cli_values: bool = False,
) -> dict[str, dict[str, str]]:
    cache_dir = args.cache_dir if use_cli_values and args and args.cache_dir else str(DEFAULT_CACHE_DIR)
    ai_provider = args.ai_provider if use_cli_values and args and args.ai_provider else DEFAULT_AI_PROVIDER
    codex_model = args.codex_model if use_cli_values and args and args.codex_model else ""
    cursor_model = args.cursor_model if use_cli_values and args and args.cursor_model else ""
    gemini_model = args.gemini_model if use_cli_values and args and args.gemini_model else ""
    max_comments = (
        args.max_comments
        if use_cli_values and args and args.max_comments is not None
        else DEFAULT_MAX_COMMENTS
    )
    smart_review = (
        args.smart_review
        if use_cli_values and args and args.smart_review is not None
        else DEFAULT_SMART_REVIEW
    )
    publish_pending = (
        args.publish_pending
        if use_cli_values and args and args.publish_pending is not None
        else DEFAULT_PUBLISH_PENDING
    )
    jira_enabled = (
        args.jira_enabled
        if use_cli_values and args and args.jira_enabled is not None
        else DEFAULT_JIRA_ENABLED
    )
    defaults = {
        "git": {
            "cache_dir": cache_dir,
            "ssh_key_path": "",
            "ssh_command": "",
        },
        "ai": {
            "provider": ai_provider,
        },
        "codex": {
            "model": codex_model,
        },
        "cursor": {
            "model": cursor_model,
        },
        "gemini": {
            "model": gemini_model,
        },
        "review": {
            "max_comments": str(max_comments),
            "smart_review": bool_config_value(smart_review),
        },
        "github": {
            "publish_pending": bool_config_value(publish_pending),
            "token": "",
        },
        "jira": {
            "enabled": bool_config_value(jira_enabled),
        },
        "telegram": {
            "bot_token": DEFAULT_TELEGRAM_BOT_TOKEN,
            "authorized_user_id": DEFAULT_TELEGRAM_AUTHORIZED_USER_ID,
            "jira_auth_via_telegram": bool_config_value(DEFAULT_TELEGRAM_JIRA_AUTH_VIA_TELEGRAM),
        },
    }
    if use_cli_values and args and args.save_prompt:
        defaults["review"]["save_prompt"] = args.save_prompt
    return defaults


def write_config_file(path: Path, parser: configparser.ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as config_file:
        parser.write(config_file)


def create_default_config_file(path: Path, args: argparse.Namespace) -> None:
    parser = configparser.ConfigParser()
    parser.read_dict(default_config_values(args, use_cli_values=True))
    write_config_file(path, parser)


def update_missing_config_defaults(path: Path, parser: configparser.ConfigParser) -> None:
    changed = False
    for section, values in default_config_values().items():
        if not parser.has_section(section):
            parser.add_section(section)
            changed = True
        for key, value in values.items():
            if not parser.has_option(section, key):
                parser.set(section, key, value)
                changed = True
    if changed:
        write_config_file(path, parser)


def build_config(args: argparse.Namespace) -> AppConfig:
    config_file = args.config_file.expanduser()
    if args.ai_provider and args.ai_provider.strip().lower() not in SUPPORTED_AI_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AI_PROVIDERS))
        raise SystemExit(f"Unsupported AI provider {args.ai_provider!r}. Supported providers: {supported}.")
    if not config_file.exists():
        create_default_config_file(config_file, args)
    parser = read_config_file(config_file)
    update_missing_config_defaults(config_file, parser)

    cache_dir_raw = args.cache_dir or config_get(parser, "git", "cache_dir")
    git_ssh_key_path_raw = config_secret_value(config_get(parser, "git", "ssh_key_path"))
    git_ssh_command = config_secret_value(config_get(parser, "git", "ssh_command"))
    ai_provider = args.ai_provider or config_get(parser, "ai", "provider") or DEFAULT_AI_PROVIDER
    codex_model = blank_to_none(args.codex_model or config_get(parser, "codex", "model"))
    cursor_model = blank_to_none(args.cursor_model or config_get(parser, "cursor", "model"))
    gemini_model = blank_to_none(args.gemini_model or config_get(parser, "gemini", "model"))
    max_comments_raw = args.max_comments if args.max_comments is not None else config_get(parser, "review", "max_comments")
    save_prompt_raw = args.save_prompt or config_get(parser, "review", "save_prompt")
    github_token = config_secret_value(config_get(parser, "github", "token") or config_get(parser, "github", "key"))
    jira = build_jira_config(args, parser)
    telegram = build_telegram_config(parser)

    publish_pending = DEFAULT_PUBLISH_PENDING
    configured_publish = config_get(parser, "github", "publish_pending")
    if configured_publish is not None:
        publish_pending = parse_bool(configured_publish)
    if args.publish_pending is not None:
        publish_pending = args.publish_pending

    smart_review = DEFAULT_SMART_REVIEW
    configured_smart_review = config_get(parser, "review", "smart_review")
    if configured_smart_review is not None:
        smart_review = parse_bool(configured_smart_review)
    if args.smart_review is not None:
        smart_review = args.smart_review

    ai_provider = ai_provider.strip().lower()
    if ai_provider not in SUPPORTED_AI_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AI_PROVIDERS))
        raise SystemExit(f"Unsupported AI provider {ai_provider!r}. Supported providers: {supported}.")

    try:
        max_comments = int(max_comments_raw) if max_comments_raw is not None else 20
    except ValueError as exc:
        raise SystemExit(f"Invalid max_comments value: {max_comments_raw!r}") from exc
    if max_comments < 1:
        raise SystemExit("max_comments must be at least 1.")
    if git_ssh_key_path_raw and git_ssh_command:
        raise SystemExit("Configure either git.ssh_key_path or git.ssh_command, not both.")
    if git_ssh_key_path_raw:
        git_ssh_command = git_ssh_command_from_key(expand_path(git_ssh_key_path_raw))

    return AppConfig(
        config_file=config_file,
        cache_dir=expand_path(cache_dir_raw) if cache_dir_raw else DEFAULT_CACHE_DIR,
        git_ssh_command=git_ssh_command,
        ai_provider=ai_provider,
        codex_model=codex_model,
        cursor_model=cursor_model,
        gemini_model=gemini_model,
        github_token=github_token,
        publish_pending=publish_pending,
        smart_review=smart_review,
        max_comments=max_comments,
        save_prompt=expand_path(save_prompt_raw) if save_prompt_raw else None,
        jira=jira,
        telegram=telegram,
    )


def build_jira_config(args: argparse.Namespace, parser: configparser.ConfigParser) -> JiraConfig:
    jira_enabled = DEFAULT_JIRA_ENABLED
    configured_enabled = config_get(parser, "jira", "enabled")
    if configured_enabled is not None:
        jira_enabled = parse_bool(configured_enabled)
    if args.jira_enabled is not None:
        jira_enabled = args.jira_enabled

    return JiraConfig(enabled=jira_enabled)


def build_telegram_config(parser: configparser.ConfigParser) -> TelegramConfig:
    bot_token = config_secret_value(config_get(parser, "telegram", "bot_token"))
    authorized_user_id_raw = blank_to_none(config_get(parser, "telegram", "authorized_user_id"))
    jira_auth_via_telegram = DEFAULT_TELEGRAM_JIRA_AUTH_VIA_TELEGRAM
    configured_jira_auth_via_telegram = config_get(parser, "telegram", "jira_auth_via_telegram")
    if configured_jira_auth_via_telegram is not None:
        jira_auth_via_telegram = parse_bool(configured_jira_auth_via_telegram)
    authorized_user_id: int | None = None
    if authorized_user_id_raw is not None:
        try:
            authorized_user_id = int(authorized_user_id_raw)
        except ValueError as exc:
            raise SystemExit(f"Invalid telegram.authorized_user_id value: {authorized_user_id_raw!r}") from exc
    return TelegramConfig(
        bot_token=bot_token,
        authorized_user_id=authorized_user_id,
        jira_auth_via_telegram=jira_auth_via_telegram,
    )


def git_ssh_command_from_key(key_path: Path) -> str:
    return (
        f"ssh -i {shlex.quote(str(key_path))} "
        "-o IdentitiesOnly=yes "
        "-o IdentityAgent=none"
    )


def git_env(git_ssh_command: str | None) -> dict[str, str] | None:
    if not git_ssh_command:
        return None
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = git_ssh_command
    return env


def gh_env(github_token: str | None) -> dict[str, str] | None:
    return gh_client.gh_env(github_token)


def gh_api(
    path: str,
    *,
    method: str = "GET",
    headers: list[str] | None = None,
    body: Any = None,
    github_token: str | None = None,
) -> str:
    return gh_client.gh_api(
        path,
        method=method,
        headers=headers,
        body=body,
        github_token=github_token,
        run_cmd=run,
        status_cb=status,
    )


def load_pr(ref: PullRequestRef, github_token: str | None = None) -> PullRequest:
    raw = gh_api(f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}", github_token=github_token)
    data = json.loads(raw)
    return PullRequest(
        ref=ref,
        title=data.get("title") or "",
        body=data.get("body") or "",
        html_url=data["html_url"],
        base_clone_url=data["base"]["repo"]["clone_url"],
        base_ssh_url=data["base"]["repo"].get("ssh_url") or data["base"]["repo"]["clone_url"],
        base_sha=data["base"]["sha"],
        head_sha=data["head"]["sha"],
        head_ref=data["head"]["ref"],
    )


def load_first_pr_comment(ref: PullRequestRef, github_token: str | None = None) -> str:
    raw = gh_api(f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments?per_page=1", github_token=github_token)
    data = json.loads(raw)
    if not data:
        return ""
    return data[0].get("body") or ""


def load_paginated_gh_api(path: str, github_token: str | None = None) -> list[Any]:
    items: list[Any] = []
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        raw = gh_api(f"{path}{separator}per_page=100&page={page}", github_token=github_token)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise SystemExit(f"Expected GitHub API list response for {path}.")
        items.extend(data)
        if len(data) < 100:
            return items
        page += 1


def trim_pull_request_context(value: str) -> str:
    if len(value) <= MAX_CONTEXT_CHARS:
        return value
    return value[:MAX_CONTEXT_CHARS] + "\n\n[PR discussion context truncated by ai-review]\n"


def load_pr_discussion_context(ref: PullRequestRef, github_token: str | None = None) -> str:
    status("Loading pull request discussion")
    parts: list[str] = []

    issue_comments = load_paginated_gh_api(
        f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
        github_token=github_token,
    )
    for item in issue_comments:
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        user = (item.get("user") or {}).get("login") or "unknown"
        created_at = item.get("created_at") or "unknown time"
        parts.append(f"PR conversation comment by {user} at {created_at}:\n{body}\n")

    reviews = load_paginated_gh_api(
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews",
        github_token=github_token,
    )
    for item in reviews:
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        user = (item.get("user") or {}).get("login") or "unknown"
        state = item.get("state") or "UNKNOWN"
        submitted_at = item.get("submitted_at") or item.get("created_at") or "unknown time"
        parts.append(f"PR review {state} by {user} at {submitted_at}:\n{body}\n")

    review_comments = load_paginated_gh_api(
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments",
        github_token=github_token,
    )
    for item in review_comments:
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        user = (item.get("user") or {}).get("login") or "unknown"
        path = item.get("path") or "unknown file"
        line = item.get("line") or item.get("original_line") or "unknown line"
        created_at = item.get("created_at") or "unknown time"
        parts.append(f"Inline review comment by {user} at {created_at} on {path}:{line}:\n{body}\n")

    return trim_pull_request_context("\n".join(parts)) if parts else "No existing PR comments or review comments were found."


def ensure_checkout(pr: PullRequest, cache_dir: Path, git_ssh_command: str | None = None) -> Path:
    repos_dir = cache_dir / "repos"
    worktrees_dir = cache_dir / "worktrees"
    repos_dir.mkdir(parents=True, exist_ok=True)
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    bare_repo = repos_dir / f"{pr.ref.slug}.git"
    clone_url = pr.base_ssh_url if git_ssh_command else pr.base_clone_url
    network_env = git_env(git_ssh_command)
    if not bare_repo.exists():
        status("Cloning repository mirror", clone_url)
        run(["git", "clone", "--mirror", clone_url, str(bare_repo)], env=network_env)
    else:
        status("Updating repository mirror", str(bare_repo))
        run(["git", "remote", "set-url", "origin", clone_url], cwd=bare_repo)
        run(["git", "fetch", "--prune", "origin"], cwd=bare_repo, env=network_env)

    status("Fetching pull request head", f"PR #{pr.ref.number}")
    run(
        [
            "git",
            "fetch",
            "origin",
            f"+refs/pull/{pr.ref.number}/head:refs/remotes/pull/{pr.ref.number}/head",
        ],
        cwd=bare_repo,
        env=network_env,
    )

    worktree = worktrees_dir / f"{pr.ref.slug}__pr-{pr.ref.number}"
    if worktree.exists():
        status("Checking existing PR worktree", str(worktree))
        current = run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).strip()
        if current == pr.head_sha:
            status("Using cached PR worktree", str(worktree))
            return worktree
        status("Refreshing PR worktree", str(worktree))
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=bare_repo)

    status("Creating PR worktree", str(worktree))
    run(["git", "worktree", "add", "--force", "--detach", str(worktree), pr.head_sha], cwd=bare_repo)
    return worktree


def load_diff(ref: PullRequestRef, github_token: str | None = None) -> str:
    return gh_api(
        f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
        headers=["Accept: application/vnd.github.v3.diff"],
        github_token=github_token,
    )


def changed_right_lines(diff: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int | None = None

    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ b/"):
            current_path = raw_line.removeprefix("+++ b/")
            changed.setdefault(current_path, set())
            continue
        if raw_line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", raw_line)
            new_line = int(match.group(1)) if match else None
            continue
        if current_path is None or new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++ "):
            changed[current_path].add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("--- "):
            continue
        else:
            new_line += 1

    return changed


def build_prompt(
    pr: PullRequest,
    diff: str,
    jira_context: str | None,
    pr_discussion_context: str,
    smart_review: bool,
) -> str:
    trimmed_diff = diff
    if len(trimmed_diff) > MAX_DIFF_CHARS:
        trimmed_diff = diff[:MAX_DIFF_CHARS] + "\n\n[Diff truncated by ai-review]\n"

    jira_section = jira_context or "No Jira context was loaded."
    context_rule = (
        "Smart review is enabled. Start from the supplied PR metadata, existing PR discussion, Jira context, and "
        "unified diff. Inspect checked-out project files only as a targeted fallback when the diff leaves an "
        "important correctness, security, data-loss, concurrency, migration, API contract, or test-gap question "
        "unclear. Prefer reading the smallest directly related files, such as changed files and immediate callers "
        "or callees. Do not scan the whole project, run broad searches without a concrete question, write files, or "
        "ask for permissions."
        if smart_review
        else "Smart review is disabled. Strictly use only the PR metadata, existing PR discussion, Jira context, "
        "and unified diff supplied in this prompt. Do not use repository files or infer from context that is not "
        "included below."
    )

    return textwrap.dedent(
        f"""
        You are reviewing a GitHub pull request. Focus on concrete correctness,
        security, data-loss, concurrency, migration, API contract, and test-gap
        issues. Avoid style comments and praise.

        Return only valid JSON with this shape:
        {{
          "summary": "one short paragraph",
          "pr_comments": [
            "optional PR-level comment for missing feature scope, incomplete ticket coverage, or broad design risk"
          ],
          "comments": [
            {{
              "path": "relative/file/path",
              "line": 123,
              "body": "actionable review comment"
            }}
          ]
        }}

        Rules:
        - {context_rule}
        - Only comment on added or changed RIGHT-side lines present in the diff.
        - Each body must explain the risk and the change needed.
        - Prefer no comment over a speculative comment.
        - Use pr_comments only for review feedback that cannot be attached to a
          specific changed line.
        - Limit to the highest-signal findings.
        - If Jira context is present, check whether the PR appears to satisfy
          the ticket requirements and note likely gaps.

        PR: {pr.html_url}
        Title: {pr.title}
        Body:
        {pr.body}

        Existing PR discussion, comments, and reviews:
        {pr_discussion_context}

        Jira context:
        {jira_section}

        Unified diff:
        ```diff
        {trimmed_diff}
        ```
        """
    ).strip()


def build_ticket_extraction_prompt(pr: PullRequest, first_comment: str) -> str:
    return textwrap.dedent(
        f"""
        Extract the Jira ticket key for this GitHub pull request.

        Return only valid JSON:
        {{"ticket_key": "ABC-123"}}

        If no ticket key is present, return:
        {{"ticket_key": null}}

        Ticket key examples:
        - Branch "ABC-123-something" means "ABC-123".
        - Branch "ABC12-123-something-short-desc" means "ABC12-123".
        - PR text may include "Refs: TICKET-123".

        Branch: {pr.head_ref}
        PR title: {pr.title}
        PR body:
        {pr.body}

        First PR comment:
        {first_comment}
        """
    ).strip()


def extract_ticket_key_heuristic(pr: PullRequest, first_comment: str) -> str | None:
    candidates = [pr.head_ref, pr.title, pr.body, first_comment]
    for candidate in candidates:
        match = TICKET_KEY_RE.search(candidate or "")
        if match:
            return match.group(1)
    return None


def extract_ticket_key_with_ai_provider(
    pr: PullRequest,
    first_comment: str,
    provider: str,
    model: str | None,
) -> str | None:
    status("Asking AI provider to identify Jira ticket", provider)
    raw = run_ai_provider(build_ticket_extraction_prompt(pr, first_comment), None, provider, model)
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        return None
    ticket_key = data.get("ticket_key")
    if not ticket_key:
        return None
    match = TICKET_KEY_RE.search(str(ticket_key))
    return match.group(1) if match else None


def extract_ticket_key(pr: PullRequest, first_comment: str, provider: str, model: str | None) -> str | None:
    return extract_ticket_key_with_ai_provider(pr, first_comment, provider, model) or extract_ticket_key_heuristic(
        pr, first_comment
    )


def mcporter_auth(server: str, timeout_ms: int) -> None:
    jira_client.mcporter_auth(server, timeout_ms, run_cmd=run, status_cb=status)


def mcporter_auth_streaming(server: str, timeout_ms: int) -> None:
    jira_client.mcporter_auth_streaming(server, timeout_ms, run_streaming_cmd=run_streaming, status_cb=status)


def mcporter_call(selector: str, args: dict[str, Any], timeout_ms: int) -> str:
    return jira_client.mcporter_call(selector, args, timeout_ms, run_cmd=run, status_cb=status)


def is_auth_error(message: str) -> bool:
    return jira_client.is_auth_error(message)


def trim_context(value: str, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n\n[Jira context truncated by ai-review]\n"


def fetch_jira_tool_context(label: str, selector: str | None, issue_arg: str, ticket_key: str, timeout_ms: int) -> str:
    return jira_client.fetch_jira_tool_context(
        label,
        selector,
        issue_arg,
        ticket_key,
        timeout_ms,
        run_cmd=run,
        status_cb=status,
        auth_handler_var=JIRA_AUTH_HANDLER,
        call_tool=mcporter_call,
        auth_func=mcporter_auth,
    )


def load_jira_context(
    config: JiraConfig,
    pr: PullRequest,
    first_comment: str,
    ai_provider: str,
    model: str | None,
) -> str | None:
    if not config.enabled:
        status("Skipping Jira context", "disabled")
        return None
    status("Checking Jira context")
    ticket_key = extract_ticket_key(pr, first_comment, ai_provider, model)
    if not ticket_key:
        status("No Jira ticket key found")
        return "Jira was enabled, but no ticket key could be extracted from the branch, PR title, PR body, or first PR comment."

    status("Found Jira ticket", ticket_key)
    parts = [f"Ticket key: {ticket_key}\n"]
    parts.append(fetch_jira_tool_context("Issue", DEFAULT_JIRA_ISSUE_TOOL, DEFAULT_JIRA_ISSUE_ARG, ticket_key, DEFAULT_MCP_TIMEOUT_MS))
    parts.append(
        fetch_jira_tool_context(
            "Comments", DEFAULT_JIRA_COMMENTS_TOOL, DEFAULT_JIRA_ISSUE_ARG, ticket_key, DEFAULT_MCP_TIMEOUT_MS
        )
    )
    parts.append(
        fetch_jira_tool_context(
            "Attachments/images",
            DEFAULT_JIRA_ATTACHMENTS_TOOL,
            DEFAULT_JIRA_ISSUE_ARG,
            ticket_key,
            DEFAULT_MCP_TIMEOUT_MS,
        )
    )
    return trim_context("\n".join(part for part in parts if part))


def run_codex(prompt: str, cwd: Path | None, model: str | None) -> str:
    return codex_provider.run_codex(prompt, cwd, model, run_cmd=run, status_cb=status)


def run_cursor(prompt: str, cwd: Path | None) -> str:
    return cursor_provider.run_cursor(prompt, cwd, None, run_cmd=run, status_cb=status)


def run_gemini(prompt: str, cwd: Path | None, model: str | None) -> str:
    return gemini_provider.run_gemini(prompt, cwd, model, run_cmd=run, status_cb=status)


def model_for_provider(config: AppConfig, provider: str) -> str | None:
    normalized_provider = provider.strip().lower()
    if normalized_provider == "codex":
        return config.codex_model
    if normalized_provider == "cursor":
        return config.cursor_model
    if normalized_provider == "gemini":
        return config.gemini_model
    return None


def run_ai_provider(prompt: str, cwd: Path | None, provider: str, model: str | None) -> str:
    normalized_provider = provider.strip().lower()
    if normalized_provider == "codex":
        return run_codex(prompt, cwd, model)
    if normalized_provider == "cursor":
        return cursor_provider.run_cursor(prompt, cwd, model, run_cmd=run, status_cb=status)
    if normalized_provider == "gemini":
        return run_gemini(prompt, cwd, model)
    supported = ", ".join(sorted(SUPPORTED_AI_PROVIDERS))
    raise SystemExit(f"Unsupported AI provider {provider!r}. Supported providers: {supported}.")


def parse_review(raw: str) -> ReviewResult:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Reviewer returned invalid JSON: {exc}") from exc

    comments: list[ReviewComment] = []
    for item in data.get("comments", []):
        try:
            comments.append(
                ReviewComment(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    body=str(item["body"]).strip(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Invalid review comment object: {item!r}") from exc

    pr_comments = [str(item).strip() for item in data.get("pr_comments", []) if str(item).strip()]
    return ReviewResult(summary=str(data.get("summary") or ""), comments=comments, pr_comments=pr_comments)


def validate_comments(review: ReviewResult, changed: dict[str, set[int]], max_comments: int) -> ReviewResult:
    valid: list[ReviewComment] = []
    rejected: list[ReviewComment] = []
    for comment in review.comments:
        if comment.body and comment.line in changed.get(comment.path, set()):
            valid.append(comment)
        else:
            rejected.append(comment)

    if rejected:
        print(f"Rejected {len(rejected)} comment(s) that did not target added RIGHT-side diff lines.", file=sys.stderr)

    return ReviewResult(summary=review.summary, comments=valid[:max_comments], pr_comments=review.pr_comments)


def pending_review_url(pr: PullRequest, review_id: int) -> str:
    return f"{pr.html_url}#pullrequestreview-{review_id}"


def create_pending_review(pr: PullRequest, review: ReviewResult, github_token: str | None = None) -> tuple[int, str]:
    status("Creating pending GitHub review", f"{len(review.comments)} inline, {len(review.pr_comments)} PR-level")
    payload = {
        "commit_id": pr.head_sha,
        "body": build_review_body(review),
        "comments": [
            {
                "path": comment.path,
                "line": comment.line,
                "side": "RIGHT",
                "body": comment.body,
            }
            for comment in review.comments
        ],
    }
    raw = gh_api(
        f"repos/{pr.ref.owner}/{pr.ref.repo}/pulls/{pr.ref.number}/reviews",
        method="POST",
        body=payload,
        github_token=github_token,
    )
    data = json.loads(raw)
    review_id = int(data["id"])
    return review_id, str(data.get("html_url") or pending_review_url(pr, review_id))


def load_authenticated_github_login(github_token: str | None = None) -> str:
    status("Loading authenticated GitHub user")
    raw = gh_api("user", github_token=github_token)
    login = json.loads(raw).get("login")
    if not login:
        raise SystemExit("Could not determine authenticated GitHub user.")
    return str(login)


def find_pending_review_id(pr: PullRequest, github_token: str | None = None) -> int | None:
    login = load_authenticated_github_login(github_token)
    status("Checking for existing pending review", login)
    pending_review_id: int | None = None
    page = 1
    while True:
        raw = gh_api(
            f"repos/{pr.ref.owner}/{pr.ref.repo}/pulls/{pr.ref.number}/reviews?per_page=100&page={page}",
            github_token=github_token,
        )
        reviews = json.loads(raw)
        for item in reviews:
            user = item.get("user") or {}
            if str(item.get("state") or "").upper() == "PENDING" and user.get("login") == login:
                pending_review_id = int(item["id"])
        if len(reviews) < 100:
            return pending_review_id
        page += 1


def delete_pending_review(pr: PullRequest, review_id: int, github_token: str | None = None) -> None:
    status("Deleting existing pending review", f"#{review_id}")
    gh_api(
        f"repos/{pr.ref.owner}/{pr.ref.repo}/pulls/{pr.ref.number}/reviews/{review_id}",
        method="DELETE",
        github_token=github_token,
    )


def create_or_replace_pending_review(
    pr: PullRequest,
    review: ReviewResult,
    github_token: str | None = None,
) -> PendingReviewResult:
    existing_review_id = find_pending_review_id(pr, github_token)
    if existing_review_id is not None:
        delete_pending_review(pr, existing_review_id, github_token)
    review_id, html_url = create_pending_review(pr, review, github_token)
    return PendingReviewResult(review_id=review_id, html_url=html_url, replaced_existing=existing_review_id is not None)


def build_review_body(review: ReviewResult) -> str:
    body = review.summary or "AI review draft."
    if review.pr_comments:
        comments = "\n\n".join(f"- {comment}" for comment in review.pr_comments)
        body = f"{body}\n\nPR-level comments:\n{comments}"
    return body


def print_review(review: ReviewResult) -> None:
    if review.summary:
        print(review.summary)
        print()
    if not review.comments:
        print("No inline comments.")
    if review.pr_comments:
        print("PR-level comments:")
        for comment in review.pr_comments:
            print(f"- {comment}")
        print()
    if not review.comments:
        return
    for index, comment in enumerate(review.comments, start=1):
        print(f"{index}. {comment.path}:{comment.line}")
        print(textwrap.indent(comment.body, "   "))
        print()


def run_review_command(args: argparse.Namespace, config: AppConfig) -> int:
    if args.jira_auth:
        mcporter_auth(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
        if not args.pr_url:
            return 0
    if not args.pr_url:
        raise SystemExit("the following arguments are required: pr_url")
    ref = parse_pr_url(args.pr_url)
    status("Loading pull request", f"{ref.owner}/{ref.repo}#{ref.number}")
    pr = load_pr(ref, config.github_token)

    pr_discussion_context = load_pr_discussion_context(ref, config.github_token)
    first_comment = load_first_pr_comment(ref, config.github_token) if config.jira.enabled else ""
    provider_model = model_for_provider(config, config.ai_provider)
    jira_context = load_jira_context(config.jira, pr, first_comment, config.ai_provider, provider_model)
    status("Loading pull request diff")
    diff = load_diff(ref, config.github_token)
    status("Building review prompt")
    prompt = build_prompt(pr, diff, jira_context, pr_discussion_context, config.smart_review)
    if config.save_prompt:
        status("Saving review prompt", str(config.save_prompt))
        config.save_prompt.parent.mkdir(parents=True, exist_ok=True)
        config.save_prompt.write_text(prompt, encoding="utf-8")

    checkout: Path | None = None
    if config.smart_review:
        checkout = ensure_checkout(pr, config.cache_dir, config.git_ssh_command)
    raw_review = run_ai_provider(prompt, checkout, config.ai_provider, provider_model)
    status("Validating reviewer findings")
    review = validate_comments(parse_review(raw_review), changed_right_lines(diff), config.max_comments)

    if checkout:
        print(f"Checked out PR head at: {checkout}")
        print()
    print_review(review)

    if config.publish_pending:
        if not review.comments and not review.pr_comments and not review.summary:
            raise SystemExit("No review content to publish.")
        result = create_or_replace_pending_review(pr, review, config.github_token)
        if result.replaced_existing:
            print(f"Updated pending GitHub review #{result.review_id}. It has not been submitted.")
        else:
            print(f"Created pending GitHub review #{result.review_id}. It has not been submitted.")
        print(f"Open pending review: {result.html_url}")

    return 0


def run_review_for_bot(pr_url: str, config: AppConfig) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return run_review_to_pending_review_url(pr_url, config)
    except SystemExit as exc:
        detail = str(exc)
        if detail:
            return detail
        return f"Review failed with exit code {exc.code}."


def run_review_for_bot_result(pr_url: str, config: AppConfig) -> tuple[bool, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return True, run_review_to_pending_review_url(pr_url, config)
    except SystemExit as exc:
        detail = str(exc) or f"Review failed with exit code {exc.code}."
        return False, ANSI_ESCAPE_RE.sub("", detail)
    except Exception as exc:
        return False, ANSI_ESCAPE_RE.sub("", f"{type(exc).__name__}: {exc}")


def provider_command(provider: str) -> str:
    if provider == "codex":
        return "codex"
    if provider == "cursor":
        return "cursor-agent"
    if provider == "gemini":
        return "gemini"
    return provider


def run_review_to_pending_review_url(pr_url: str, config: AppConfig) -> str:
    if not config.publish_pending:
        raise SystemExit("telegram bot mode requires github.publish_pending = yes so it can return a pending review link.")
    ref = parse_pr_url(pr_url)
    status("Loading pull request", f"{ref.owner}/{ref.repo}#{ref.number}")
    pr = load_pr(ref, config.github_token)

    pr_discussion_context = load_pr_discussion_context(ref, config.github_token)
    first_comment = load_first_pr_comment(ref, config.github_token) if config.jira.enabled else ""
    provider_model = model_for_provider(config, config.ai_provider)
    jira_context = load_jira_context(config.jira, pr, first_comment, config.ai_provider, provider_model)
    status("Loading pull request diff")
    diff = load_diff(ref, config.github_token)
    status("Building review prompt")
    prompt = build_prompt(pr, diff, jira_context, pr_discussion_context, config.smart_review)
    if config.save_prompt:
        status("Saving review prompt", str(config.save_prompt))
        config.save_prompt.parent.mkdir(parents=True, exist_ok=True)
        config.save_prompt.write_text(prompt, encoding="utf-8")

    checkout: Path | None = None
    if config.smart_review:
        checkout = ensure_checkout(pr, config.cache_dir, config.git_ssh_command)
    raw_review = run_ai_provider(prompt, checkout, config.ai_provider, provider_model)
    status("Validating reviewer findings")
    review = validate_comments(parse_review(raw_review), changed_right_lines(diff), config.max_comments)
    if not review.comments and not review.pr_comments and not review.summary:
        raise SystemExit("No review content to publish.")
    result = create_or_replace_pending_review(pr, review, config.github_token)
    return result.html_url


def chunk_telegram_message(message: str, limit: int = 3900) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks = []
    remaining = message
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def run_telegram_bot(config: AppConfig) -> int:
    if not config.telegram.bot_token:
        raise SystemExit("Configure telegram.bot_token before running `ai-review bot`.")
    try:
        from telegram import Update
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes, MessageHandler, filters
    except ImportError as exc:
        raise SystemExit(
            "Telegram bot support is not installed. Install it with `python3 -m pip install 'ai-review[bot]'`."
        ) from exc

    available_providers = [
        provider for provider in sorted(SUPPORTED_AI_PROVIDERS) if shutil.which(provider_command(provider))
    ]
    if not available_providers:
        raise SystemExit("No supported AI provider command found in PATH. Install one of: codex, cursor, gemini.")
    selected_provider_by_user: dict[int, str] = {}

    def selected_provider_for_user(user_id: int) -> str:
        selected = selected_provider_by_user.get(user_id) or config.ai_provider
        if selected not in available_providers:
            return available_providers[0]
        return selected

    def provider_keyboard(user_id: int) -> InlineKeyboardMarkup:
        current = selected_provider_for_user(user_id)
        row = []
        for provider in available_providers:
            label = provider
            if provider == current:
                label = f"{label} [current]"
            row.append(InlineKeyboardButton(label, callback_data=f"provider:{provider}"))
        return InlineKeyboardMarkup([row])

    async def show_provider_prompt(message_obj: Any, user_id: int) -> None:
        await message_obj.reply_text("choose model or paste PR link", reply_markup=provider_keyboard(user_id))

    async def handle_provider_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        user_id = query.from_user.id
        authorized_user_id = config.telegram.authorized_user_id
        if authorized_user_id is not None and user_id != authorized_user_id:
            await query.answer()
            return
        data = query.data or ""
        if not data.startswith("provider:"):
            await query.answer()
            return
        provider = data.split(":", 1)[1].strip().lower()
        if provider not in available_providers:
            await query.answer("Provider command not found.", show_alert=False)
            return
        selected_provider_by_user[user_id] = provider
        await query.answer(f"Provider set to {provider}")
        await query.edit_message_text("choose model or paste PR link", reply_markup=provider_keyboard(user_id))

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        authorized_user_id = config.telegram.authorized_user_id
        if authorized_user_id is None:
            await update.message.reply_text(f"Your Telegram user ID is {user_id}.")
            return
        if user_id != authorized_user_id:
            return
        text = (update.message.text or "").strip()
        if not text or not GITHUB_PR_RE.match(text):
            await show_provider_prompt(update.message, user_id)
            return
        selected_provider = selected_provider_for_user(user_id)
        review_config = replace(config, ai_provider=selected_provider)
        progress_message = await update.message.reply_text("Review started.")
        progress_lines = ["Review started."]
        loop = asyncio.get_running_loop()

        async def update_progress(line: str) -> None:
            progress_lines.append(line)
            visible = "\n".join(progress_lines[-35:])
            if len(visible) > 3900:
                visible = visible[-3900:]
            try:
                await progress_message.edit_text(visible)
            except Exception:
                pass

        def progress(line: str) -> None:
            clean_line = sanitize_telegram_progress_line(line)
            if not clean_line:
                return
            future = asyncio.run_coroutine_threadsafe(update_progress(clean_line), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass

        status_token = STATUS_LISTENER.set(progress)
        jira_auth_token = None
        if config.telegram.jira_auth_via_telegram:
            jira_auth_token = JIRA_AUTH_HANDLER.set(
                lambda: mcporter_auth_streaming(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
            )
        try:
            ok, output = await asyncio.to_thread(run_review_for_bot_result, text, review_config)
        finally:
            STATUS_LISTENER.reset(status_token)
            if jira_auth_token is not None:
                JIRA_AUTH_HANDLER.reset(jira_auth_token)

        if ok:
            # On success, replace progress with only the pending review URL.
            try:
                await progress_message.edit_text(output.strip())
            except Exception:
                await update.message.reply_text(output.strip())
            return

        await update_progress("Review failed.")
        for chunk in chunk_telegram_message(output):
            await update.message.reply_text(chunk)

    application = ApplicationBuilder().token(config.telegram.bot_token).build()
    application.add_handler(CallbackQueryHandler(handle_provider_button, pattern=r"^provider:"))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    status("Starting Telegram bot")
    application.run_polling()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an AI review for a GitHub pull request.",
        epilog="Subcommands: bot  Run as a Telegram bot.",
    )
    parser.add_argument("pr_url", nargs="?", help="GitHub pull request URL, or `bot` to run the Telegram bot")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=f"Config file path. Defaults to {DEFAULT_CONFIG_FILE}",
    )
    parser.add_argument(
        "--cache-dir",
        help=f"Repository/worktree cache directory. Defaults to {DEFAULT_CACHE_DIR}",
    )
    parser.add_argument(
        "--ai-provider",
        choices=sorted(SUPPORTED_AI_PROVIDERS),
        help="AI provider type to use. Defaults to codex. Supported: codex, cursor, gemini.",
    )
    parser.add_argument("--codex-model", help="Optional model passed to `codex exec --model` (codex provider only).")
    parser.add_argument("--cursor-model", help="Optional model passed to `cursor-agent --model` (cursor provider only).")
    parser.add_argument("--gemini-model", help="Optional model passed to `gemini --model` (gemini provider only).")
    parser.add_argument(
        "--publish-pending",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Create an unsubmitted GitHub pending review with the generated comments.",
    )
    parser.add_argument("--max-comments", type=int, help="Maximum inline comments to keep.")
    parser.add_argument(
        "--smart-review",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow the AI reviewer to read files from the checked-out PR worktree for extra context.",
    )
    parser.add_argument(
        "--save-prompt",
        help="Write the generated reviewer prompt to this file.",
    )
    parser.add_argument(
        "--jira-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Load Jira ticket context through mcporter before reviewing.",
    )
    parser.add_argument(
        "--jira-auth",
        action="store_true",
        help="Run `mcporter auth jira`, then exit if no PR URL is provided.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args)
    if args.pr_url == "bot":
        return run_telegram_bot(config)
    if not args.pr_url and not args.jira_auth:
        parser.error("the following arguments are required: pr_url")
    return run_review_command(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
