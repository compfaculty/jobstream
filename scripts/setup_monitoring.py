"""Create restricted local credentials; copy through stdin, never command arguments."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    "compose.yaml",
    "-f",
    "compose.monitoring.yaml",
    "--profile",
    "monitoring",
]


def main():
    parser = argparse.ArgumentParser()
    optional = parser.add_mutually_exclusive_group()
    optional.add_argument("--infinity", dest="infinity", action="store_true")
    optional.add_argument("--without-infinity", dest="infinity", action="store_false")
    parser.set_defaults(infinity=None)
    parser.add_argument("--rotate", choices=("grafana", "scrape"))
    args = parser.parse_args()
    directory = ROOT / ".monitoring-secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    values = {}
    for name in ("grafana-admin", "scrape-token"):
        path = directory / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            path.chmod(0o600)
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
        values[name] = path.read_text().strip()
    if args.rotate:
        name = "grafana-admin" if args.rotate == "grafana" else "scrape-token"
        value = secrets.token_urlsafe(48)
        if args.rotate == "grafana":
            subprocess.run(
                [
                    *COMPOSE,
                    "exec",
                    "-T",
                    "grafana",
                    "grafana",
                    "cli",
                    "--homepath",
                    "/usr/share/grafana",
                    "admin",
                    "reset-admin-password",
                    "--password-from-stdin",
                ],
                input=value + "\n",
                text=True,
                check=True,
                cwd=ROOT,
            )
        temporary = directory / (name + ".new")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value + "\n")
        temporary.replace(directory / name)
        values[name] = value
        values["rotate"] = [name]
    target = directory / "infinity-targets.json"
    if args.infinity is not None or not target.exists():
        target.write_text(
            ('[{"targets":["infinity:9100"],"labels":{"service":"infinity"}}]\n')
            if args.infinity
            else "[]\n"
        )
    target.chmod(0o644)  # Non-secret target configuration only.
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.monitoring.yaml",
            "--profile",
            "monitoring",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "monitoring-init",
        ],
        input=json.dumps(values),
        text=True,
        check=True,
        cwd=ROOT,
    )
    print("Monitoring credentials initialized (values hidden).")


if __name__ == "__main__":
    main()
