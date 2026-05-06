from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Callable

DEFAULT_JIRA_MCP_SERVER = "jira"
DEFAULT_MCP_TIMEOUT_MS = 60_000
JIRA_REMOTE_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
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


def uses_configured_jira_server(run_cmd: Callable[..., str]) -> bool:
    try:
        run_cmd(["mcporter", "list", DEFAULT_JIRA_MCP_SERVER])
        return True
    except SystemExit:
        return False


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
    if server == DEFAULT_JIRA_MCP_SERVER and uses_configured_jira_server(run_cmd):
        return []
    return jira_remote_stdio_args(server)


def normalize_jira_unavailable_error(message: str) -> str:
    return (
        "Jira MCP is unavailable. "
        "Configure MCPorter server 'jira' or allow fallback via "
        f"`npx -y mcp-remote@latest {JIRA_REMOTE_MCP_URL}`. "
        f"Original error: {message}"
    )


def mcporter_auth(server: str, timeout_ms: int, *, run_cmd: Callable[..., str], status_cb: Callable[[str, str | None], None]) -> None:
    status_cb("Authenticating Jira MCP server", server)
    target_args = jira_target_args(run_cmd, server)
    if target_args:
        status_cb("Using fallback Jira MCP source", JIRA_REMOTE_MCP_URL)
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
    target_args = jira_target_args(run_cmd, server)
    if target_args:
        status_cb("Using fallback Jira MCP source", JIRA_REMOTE_MCP_URL)
    return run_cmd(
        [
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
    )


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
    try:
        raw = call_tool(selector, {issue_arg: ticket_key}, timeout_ms)
    except SystemExit as exc:
        if is_auth_error(str(exc)):
            try:
                auth_handler = auth_handler_var.get()
                if auth_handler:
                    auth_handler()
                else:
                    auth_func(DEFAULT_JIRA_MCP_SERVER, DEFAULT_MCP_TIMEOUT_MS)
                raw = call_tool(selector, {issue_arg: ticket_key}, timeout_ms)
            except SystemExit as retry_exc:
                raise SystemExit(normalize_jira_unavailable_error(str(retry_exc))) from retry_exc
            return f"{label} ({selector}):\n{raw}\n"
        raise SystemExit(normalize_jira_unavailable_error(str(exc))) from exc
    return f"{label} ({selector}):\n{raw}\n"
