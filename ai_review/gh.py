from __future__ import annotations

import json
import os
from typing import Any, Callable


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
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    cmd = ["gh", "api"]
    for header in headers or []:
        cmd.extend(["-H", header])
    if method != "GET":
        cmd.extend(["--method", method])
    if body is not None:
        cmd.extend(["--input", "-"])
    cmd.append(path)
    status_cb("Calling GitHub API", f"{method} {path}")
    return run_cmd(cmd, input_text=json.dumps(body) if body is not None else None, env=gh_env(github_token))
