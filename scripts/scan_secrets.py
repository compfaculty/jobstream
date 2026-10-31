"""Scan tracked and unignored source files, excluding local secrets and dependencies."""

import shutil
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
files = (
    subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    )
    .decode()
    .split("\0")
)
with tempfile.TemporaryDirectory(prefix="jobstream-source-", dir="/tmp") as directory:
    target = Path(directory)
    for name in files:
        if not name:
            continue
        source = root / name
        if source.is_symlink():
            raise SystemExit(f"Refusing symlink in source scan: {name}")
        if source.is_file():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{target}:/repo:ro",
            "zricethezav/gitleaks:v8.24.2@sha256:b5918eb91b8d2473cec722f066abb4352e4ffdc4ec9f4283ec143aba9ec9ebc4",
            "dir",
            "/repo",
            "--redact",
            "--no-banner",
        ],
        check=True,
    )
