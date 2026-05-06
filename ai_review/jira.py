from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Callable

DEFAULT_JIRA_MCP_SERVER = "jira"
DEFAULT_MCP_TIMEOUT_MS = 60_000
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


def mcporter_auth(server: str, timeout_ms: int, *, run_cmd: Callable[..., str], status_cb: Callable[[str, str | None], None]) -> None:
    status_cb("Authenticating Jira MCP server", server)
    run_cmd(["mcporter", "--oauth-timeout", str(timeout_ms), "auth", server])


def mcporter_auth_streaming(
    server: str,
    timeout_ms: int,
    *,
    run_streaming_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> None:
    status_cb("Authenticating Jira MCP server", server)
    status_cb("Follow any MCPorter auth URL or instructions shown below", None)
    run_streaming_cmd(["mcporter", "--oauth-timeout", str(timeout_ms), "auth", server])


def mcporter_call(
    selector: str,
    args: dict[str, Any],
    timeout_ms: int,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    status_cb("Calling Jira MCP tool", selector)
    return run_cmd(
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
                return f"{label}: failed to load through {selector} after Jira auth retry: {retry_exc}\n"
            return f"{label} ({selector}):\n{raw}\n"
        return f"{label}: failed to load through {selector}: {exc}\n"
    return f"{label} ({selector}):\n{raw}\n"
