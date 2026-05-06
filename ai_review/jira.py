from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any, Callable

DEFAULT_JIRA_MCP_SERVER = "jira"
DEFAULT_MCP_TIMEOUT_MS = 60_000
JIRA_REMOTE_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
JIRA_RESOURCE_TOOL = "getAccessibleAtlassianResources"
JIRA_ISSUE_TOOL = "getJiraIssue"
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
_CACHED_CLOUD_ID: str | None = None


def jira_remote_stdio_args(server: str) -> list[str]:
    return [
        "--stdio",
        "npx",
        "--stdio-arg",
        "-y",
        "--stdio-arg",
        "mcp-remote@latest",
        "--stdio-arg",
        JIRA_REMOTE_MCP_URL,
        "--name",
        server,
    ]


def split_selector(selector: str) -> tuple[str, str]:
    if "." not in selector:
        raise SystemExit(f"Invalid Jira MCP selector: {selector!r}. Expected server.tool format.")
    server, tool = selector.split(".", 1)
    server = server.strip()
    tool = tool.strip()
    if not server or not tool:
        raise SystemExit(f"Invalid Jira MCP selector: {selector!r}. Expected server.tool format.")
    return server, tool


def jira_target_args(run_cmd: Callable[..., str], server: str) -> list[str]:
    _ = run_cmd
    return jira_remote_stdio_args(server)


def normalize_jira_unavailable_error(message: str) -> str:
    return (
        "Jira MCP is unavailable. "
        f"Ensure this command works on the bot host: `npx -y mcp-remote@latest {JIRA_REMOTE_MCP_URL}`. "
        f"Original error: {message}"
    )


def _find_first_string_key(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            found = _find_first_string_key(nested, keys)
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _find_first_string_key(item, keys)
            if found:
                return found
    return None


def jira_issue_title_from_raw(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if data is not None:
        # Prefer issue summary/title fields first, then fallback to any title-like key.
        title = _find_first_string_key(data, ("summary", "title"))
        if title:
            if len(title) > 140:
                return f"{title[:137]}..."
            return title
    # mcporter may print non-JSON object-ish text. Try resilient text extraction.
    for pattern in (
        r'["\']summary["\']\s*:\s*["\']([^"\']+)["\']',
        r'["\']title["\']\s*:\s*["\']([^"\']+)["\']',
        r"\bsummary\s*:\s*'([^']+)'",
        r"\btitle\s*:\s*'([^']+)'",
    ):
        match = re.search(pattern, raw, flags=re.IGNOABCASE)
        if match:
            title = match.group(1).strip()
            if len(title) > 140:
                return f"{title[:137]}..."
            return title
    return None


def mcporter_error_from_raw(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if data.get("isError") is True:
            content = data.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            return text.strip()
            return "Jira MCP call returned isError=true."
    if re.search(r"\bisError\s*:\s*true\b", raw):
        text_match = re.search(r"\btext\s*:\s*'([^']+)'", raw)
        if text_match:
            return text_match.group(1).strip()
        return "Jira MCP call returned isError=true."
    if "Unknown MCP server" in raw:
        return raw.strip()
    return None


def mcporter_auth(server: str, timeout_ms: int, *, run_cmd: Callable[..., str], status_cb: Callable[[str, str | None], None]) -> None:
    status_cb("Authenticating Jira MCP server", server)
    target_args = jira_target_args(run_cmd, server)
    status_cb("Using Jira MCP source", JIRA_REMOTE_MCP_URL)
    run_cmd(["mcporter", "--oauth-timeout", str(timeout_ms), "auth", server, *target_args])


def mcporter_auth_streaming(
    server: str,
    timeout_ms: int,
    *,
    run_streaming_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> None:
    status_cb("Authenticating Jira MCP server", server)
    status_cb("Follow any MCPorter auth URL or instructions shown below", None)
    # Streaming auth is used from bot progress updates. Keep command explicit so
    # users can complete OAuth remotely when provider supports it.
    run_streaming_cmd(
        [
            "mcporter",
            "--oauth-timeout",
            str(timeout_ms),
            "auth",
            server,
            *jira_remote_stdio_args(server),
        ]
    )


def mcporter_call(
    selector: str,
    args: dict[str, Any],
    timeout_ms: int,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    status_cb("Calling Jira MCP tool", selector)
    server, tool = split_selector(selector)
    if server == DEFAULT_JIRA_MCP_SERVER and tool in {"get_issue", "get_comments", "get_attachments"}:
        tool = JIRA_ISSUE_TOOL
    target_args = jira_target_args(run_cmd, server)
    status_cb("Using Jira MCP source", JIRA_REMOTE_MCP_URL)
    cmd = [
        "mcporter",
        "call",
        "--server",
        server,
        "--tool",
        tool,
        *target_args,
        "--args",
        json.dumps(args),
        "--timeout",
        str(timeout_ms),
        "--output",
        "json",
    ]
    output = run_cmd(cmd)
    tool_error = mcporter_error_from_raw(output)
    if tool_error:
        raise SystemExit(tool_error)
    return output


def jira_cloud_id(run_cmd: Callable[..., str], status_cb: Callable[[str, str | None], None]) -> str:
    global _CACHED_CLOUD_ID
    if _CACHED_CLOUD_ID:
        return _CACHED_CLOUD_ID
    status_cb("Resolving Jira cloud resource", None)
    raw = mcporter_call(f"{DEFAULT_JIRA_MCP_SERVER}.{JIRA_RESOURCE_TOOL}", {}, DEFAULT_MCP_TIMEOUT_MS, run_cmd=run_cmd, status_cb=status_cb)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid Jira resource response: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise SystemExit("No accessible Atlassian resources were returned.")
    for item in data:
        if isinstance(item, dict):
            scopes = item.get("scopes")
            cloud_id = item.get("id")
            if isinstance(cloud_id, str) and isinstance(scopes, list) and any("jira" in str(scope) for scope in scopes):
                _CACHED_CLOUD_ID = cloud_id
                return cloud_id
    first = data[0]
    if isinstance(first, dict) and isinstance(first.get("id"), str):
        _CACHED_CLOUD_ID = first["id"]
        return _CACHED_CLOUD_ID
    raise SystemExit("Could not determine Jira cloudId from accessible Atlassian resources.")


def is_auth_error(message: str) -> bool:
    normalized = message.lower()
    return any(pattern in normalized for pattern in AUTH_ERROR_PATTERNS)


def fetch_jira_tool_context(
    label: str,
    selector: str | None,
    issue_arg: str,
    ticket_key: str,
    timeout_ms: int,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
    auth_handler_var: ContextVar[Callable[[], None] | None],
    call_tool: Callable[[str, dict[str, Any], int], str] | None = None,
    auth_func: Callable[[str, int], None] | None = None,
) -> str:
    if not selector:
        return ""
    status_cb("Loading Jira context", f"{label} for {ticket_key}")
    call_tool = call_tool or (lambda sel, a, t: mcporter_call(sel, a, t, run_cmd=run_cmd, status_cb=status_cb))
    auth_func = auth_func or (
        lambda server, timeout: mcporter_auth(server, timeout, run_cmd=run_cmd, status_cb=status_cb)
    )
    args_payload: dict[str, Any] = {issue_arg: ticket_key}
    if selector.endswith(f".{JIRA_ISSUE_TOOL}"):
        args_payload["cloudId"] = jira_cloud_id(run_cmd, status_cb)
        normalized_label = label.strip().lower()
        if normalized_label == "issue":
            args_payload["fields"] = ["summary", "description", "status", "priority", "issuetype", "comment", "attachment"]
        elif normalized_label == "comments":
            args_payload["fields"] = ["comment"]
        elif normalized_label == "attachments/images":
            args_payload["fields"] = ["attachment"]
    try:
        raw = call_tool(selector, args_payload, timeout_ms)
        if label.strip().lower() == "issue":
            issue_title = jira_issue_title_from_raw(raw)
            if issue_title:
                status_cb("Loaded Jira issue title", issue_title)
    except SystemExit as exc:
        if is_auth_error(str(exc)):
            try:
                auth_handler = auth_handler_var.get()
                if auth_handler:
                    auth_handler()
                else:
                    auth_func(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
                raw = call_tool(selector, args_payload, timeout_ms)
                if label.strip().lower() == "issue":
                    issue_title = jira_issue_title_from_raw(raw)
                    if issue_title:
                        status_cb("Loaded Jira issue title", issue_title)
            except SystemExit as retry_exc:
                raise SystemExit(normalize_jira_unavailable_error(str(retry_exc))) from retry_exc
            return f"{label} ({selector}):\n{raw}\n"
        raise SystemExit(normalize_jira_unavailable_error(str(exc))) from exc
    return f"{label} ({selector}):\n{raw}\n"
