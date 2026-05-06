from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable


def run_codex(
    prompt: str,
    cwd: Path | None,
    model: str | None,
    *,
    run_cmd: Callable[..., str],
    status_cb: Callable[[str, str | None], None],
) -> str:
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
            "--skip-git-repo-check",
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
        status_cb("Running Codex AI provider", f"model {model}" if model else None)
        stdout = run_cmd(cmd, input_text=prompt)
        if output_file.exists():
            return output_file.read_text(encoding="utf-8")
        return stdout
