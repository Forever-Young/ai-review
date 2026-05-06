from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable


def run_claude(
    prompt: str,
    cwd: Path | None,
    model: str | None,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-review-claude-") as temp_dir:
        if cwd is None:
            cwd = Path(temp_dir) / "empty-workspace"
            cwd.mkdir()
        cmd = ["claude", "--print", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        status_cb("Running Claude AI provider", f"model {model}" if model else None)
        return run_cmd(cmd, cwd=cwd)
