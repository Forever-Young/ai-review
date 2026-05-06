from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable


def run_cursor(
    prompt: str,
    cwd: Path | None,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-review-cursor-") as temp_dir:
        if cwd is None:
            cwd = Path(temp_dir) / "empty-workspace"
            cwd.mkdir()
        status_cb("Running Cursor reviewer", None)
        return run_cmd(["cursor"], cwd=cwd, input_text=prompt)
