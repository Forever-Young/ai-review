from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable


def run_gemini(
    prompt: str,
    cwd: Path | None,
    model: str | None,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-review-gemini-") as temp_dir:
        if cwd is None:
            cwd = Path(temp_dir) / "empty-workspace"
            cwd.mkdir()
        cmd = ["gemini"]
        if model:
            cmd.extend(["--model", model])
        status_cb("Running Gemini AI provider", f"model {model}" if model else None)
        env = os.environ.copy()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        return run_cmd(cmd, cwd=cwd, input_text=prompt, env=env)
