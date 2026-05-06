from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GITHUB_PR_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
MAX_DIFF_CHARS = 180_000
DEFAULT_CONFIG_FILE = Path("~/.config/ai-review/ai-review.conf").expanduser()
DEFAULT_CACHE_DIR = Path("~/.local/cache/ai-review").expanduser()
DEFAULT_AI_PROVIDER = "codex"
DEFAULT_PUBLISH_PENDING = True
DEFAULT_SMART_REVIEW = False
SUPPORTED_AI_PROVIDERS = {"codex"}
DEFAULT_JIRA_ENABLED = False
DEFAULT_JIRA_MCP_SERVER = "jira"
DEFAULT_JIRA_ISSUE_TOOL = "jira.get_issue"
DEFAULT_JIRA_ISSUE_ARG = "issueKey"
DEFAULT_JIRA_COMMENTS_TOOL = "jira.get_comments"
DEFAULT_JIRA_ATTACHMENTS_TOOL = "jira.get_attachments"
DEFAULT_MCP_TIMEOUT_MS = 60_000
MAX_CONTEXT_CHARS = 60_000
TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
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


def status(message: str, detail: str | None = None) -> None:
    line = f"{STATUS_COLOR}[ai-review]{RESET_COLOR} {message}"
    if detail:
        line = f"{line} {DETAIL_COLOR}{detail}{RESET_COLOR}"
    print(line, file=sys.stderr)


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
class AppConfig:
    config_file: Path
    cache_dir: Path
    git_ssh_command: str | None
    ai_provider: str
    codex_model: str | None
    github_token: str | None
    publish_pending: bool
    smart_review: bool
    max_comments: int
    save_prompt: Path | None
    jira: JiraConfig


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


def create_default_config_file(path: Path, args: argparse.Namespace) -> None:
    parser = configparser.ConfigParser()
    parser["git"] = {
        "cache_dir": args.cache_dir or str(DEFAULT_CACHE_DIR),
        "ssh_key_path": "",
        "ssh_command": "",
    }
    parser["ai"] = {
        "provider": args.ai_provider or DEFAULT_AI_PROVIDER,
    }
    parser["codex"] = {
        "model": args.codex_model or "",
    }
    parser["review"] = {
        "max_comments": str(args.max_comments if args.max_comments is not None else 20),
        "smart_review": bool_config_value(
            args.smart_review if args.smart_review is not None else DEFAULT_SMART_REVIEW
        ),
    }
    if args.save_prompt:
        parser["review"]["save_prompt"] = args.save_prompt
    parser["github"] = {
        "publish_pending": bool_config_value(
            args.publish_pending if args.publish_pending is not None else DEFAULT_PUBLISH_PENDING
        ),
        "token": "",
    }
    parser["jira"] = {
        "enabled": bool_config_value(args.jira_enabled if args.jira_enabled is not None else DEFAULT_JIRA_ENABLED),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as config_file:
        parser.write(config_file)


def build_config(args: argparse.Namespace) -> AppConfig:
    config_file = args.config_file.expanduser()
    if args.ai_provider and args.ai_provider.strip().lower() not in SUPPORTED_AI_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AI_PROVIDERS))
        raise SystemExit(f"Unsupported AI provider {args.ai_provider!r}. Supported providers: {supported}.")
    if not config_file.exists():
        create_default_config_file(config_file, args)
    parser = read_config_file(config_file)

    cache_dir_raw = args.cache_dir or config_get(parser, "git", "cache_dir")
    git_ssh_key_path_raw = config_secret_value(config_get(parser, "git", "ssh_key_path"))
    git_ssh_command = config_secret_value(config_get(parser, "git", "ssh_command"))
    ai_provider = args.ai_provider or config_get(parser, "ai", "provider") or DEFAULT_AI_PROVIDER
    codex_model = blank_to_none(args.codex_model or config_get(parser, "codex", "model"))
    max_comments_raw = args.max_comments if args.max_comments is not None else config_get(parser, "review", "max_comments")
    save_prompt_raw = args.save_prompt or config_get(parser, "review", "save_prompt")
    github_token = config_secret_value(config_get(parser, "github", "token") or config_get(parser, "github", "key"))
    jira = build_jira_config(args, parser)

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
        github_token=github_token,
        publish_pending=publish_pending,
        smart_review=smart_review,
        max_comments=max_comments,
        save_prompt=expand_path(save_prompt_raw) if save_prompt_raw else None,
        jira=jira,
    )


def build_jira_config(args: argparse.Namespace, parser: configparser.ConfigParser) -> JiraConfig:
    jira_enabled = DEFAULT_JIRA_ENABLED
    configured_enabled = config_get(parser, "jira", "enabled")
    if configured_enabled is not None:
        jira_enabled = parse_bool(configured_enabled)
    if args.jira_enabled is not None:
        jira_enabled = args.jira_enabled

    return JiraConfig(enabled=jira_enabled)


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
    if not github_token:
        return None
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_TOKEN"] = github_token
    return env


def gh_api(
    path: str,
    *,
    method: str = "GET",
    headers: list[str] | None = None,
    body: Any = None,
    github_token: str | None = None,
) -> str:
    cmd = ["gh", "api"]
    for header in headers or []:
        cmd.extend(["-H", header])
    if method != "GET":
        cmd.extend(["--method", method])
    if body is not None:
        cmd.extend(["--input", "-"])
    cmd.append(path)
    status("Calling GitHub API", f"{method} {path}")
    return run(cmd, input_text=json.dumps(body) if body is not None else None, env=gh_env(github_token))


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


def extract_ticket_key_with_codex(pr: PullRequest, first_comment: str, model: str | None) -> str | None:
    status("Asking Codex to identify Jira ticket")
    raw = run_codex(build_ticket_extraction_prompt(pr, first_comment), None, model)
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        return None
    ticket_key = data.get("ticket_key")
    if not ticket_key:
        return None
    match = TICKET_KEY_RE.search(str(ticket_key))
    return match.group(1) if match else None


def extract_ticket_key(pr: PullRequest, first_comment: str, model: str | None) -> str | None:
    return extract_ticket_key_with_codex(pr, first_comment, model) or extract_ticket_key_heuristic(pr, first_comment)


def mcporter_auth(server: str, timeout_ms: int) -> None:
    status("Authenticating Jira MCP server", server)
    run(["mcporter", "--oauth-timeout", str(timeout_ms), "auth", server])


def mcporter_call(selector: str, args: dict[str, Any], timeout_ms: int) -> str:
    status("Calling Jira MCP tool", selector)
    return run(
        [
            "mcporter",
            "call",
            selector,
            "--args",
            json.dumps(args),
            "--timeout",
            str(timeout_ms),
            "--output",
            "json",
        ]
    )


def is_auth_error(message: str) -> bool:
    normalized = message.lower()
    return any(pattern in normalized for pattern in AUTH_ERROR_PATTERNS)


def trim_context(value: str, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n\n[Jira context truncated by ai-review]\n"


def fetch_jira_tool_context(label: str, selector: str | None, issue_arg: str, ticket_key: str, timeout_ms: int) -> str:
    if not selector:
        return ""
    status("Loading Jira context", f"{label} for {ticket_key}")
    try:
        raw = mcporter_call(selector, {issue_arg: ticket_key}, timeout_ms)
    except SystemExit as exc:
        if is_auth_error(str(exc)):
            try:
                mcporter_auth(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
                raw = mcporter_call(selector, {issue_arg: ticket_key}, timeout_ms)
            except SystemExit as retry_exc:
                return f"{label}: failed to load through {selector} after Jira auth retry: {retry_exc}\n"
            return f"{label} ({selector}):\n{raw}\n"
        return f"{label}: failed to load through {selector}: {exc}\n"
    return f"{label} ({selector}):\n{raw}\n"


def load_jira_context(config: JiraConfig, pr: PullRequest, first_comment: str, model: str | None) -> str | None:
    if not config.enabled:
        status("Skipping Jira context", "disabled")
        return None
    status("Checking Jira context")
    ticket_key = extract_ticket_key(pr, first_comment, model)
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
    with tempfile.TemporaryDirectory(prefix="ai-review-codex-") as temp_dir:
        if cwd is None:
            cwd = Path(temp_dir) / "empty-workspace"
            cwd.mkdir()
        output_file = Path(temp_dir) / "review.json"
        cmd = [
            "codex",
            "exec",
            "--config",
            'approval_policy="never"',
            "-C",
            str(cwd),
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(output_file),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")
        status("Running Codex reviewer", f"model {model}" if model else None)
        stdout = run(cmd, input_text=prompt)
        if output_file.exists():
            return output_file.read_text(encoding="utf-8")
        return stdout


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an AI review for a GitHub pull request.")
    parser.add_argument("pr_url", nargs="?", help="GitHub pull request URL")
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
        help="AI provider type to use. Defaults to codex.",
    )
    parser.add_argument("--codex-model", help="Optional model passed to `codex exec --model`.")
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
    if args.jira_auth:
        mcporter_auth(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
        if not args.pr_url:
            return 0
    if not args.pr_url:
        parser.error("the following arguments are required: pr_url")
    ref = parse_pr_url(args.pr_url)
    status("Loading pull request", f"{ref.owner}/{ref.repo}#{ref.number}")
    pr = load_pr(ref, config.github_token)

    pr_discussion_context = load_pr_discussion_context(ref, config.github_token)
    first_comment = load_first_pr_comment(ref, config.github_token) if config.jira.enabled else ""
    jira_context = load_jira_context(config.jira, pr, first_comment, config.codex_model)
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
    raw_review = run_codex(prompt, checkout, config.codex_model)
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


if __name__ == "__main__":
    raise SystemExit(main())
