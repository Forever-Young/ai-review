from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable


def run_cursor(
    prompt: str,
    cwd: Path | None,
    model: str | None,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-review-cursor-") as temp_dir:
        if cwd is None:
            cwd = Path(temp_dir) / "empty-workspace"
            cwd.mkdir()
        cmd = ["cursor"]
        if model:
            cmd.extend(["--model", model])
        status_cb("Running Cursor reviewer", f"model {model}" if model else None)
        return run_cmd(cmd, cwd=cwd, input_text=prompt)
