"""Initialize only dedicated telemetry volumes; receive credentials over stdin."""

import json
import os
import sys
from pathlib import Path

for directory in Path("/monitoring-volumes").iterdir():
    os.chown(directory, 10001, 10001)
    directory.chmod(0o750)
values = json.loads(sys.stdin.read(4096))
for name, directory in [("grafana-admin", "/grafana-secret"), ("scrape-token", "/scrape-secret")]:
    value = values[name]
    if not isinstance(value, str) or not 32 <= len(value) <= 128 or not value.isascii():
        raise ValueError("Invalid credential shape")
    os.chown(directory, 0, 0)
    path = Path(directory) / "credential"
    if not path.exists() or name in values.get("rotate", []):
        temporary = Path(directory) / "credential.new"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        os.chown(temporary, 10001, 10001)
        temporary.chmod(0o400)
        temporary.replace(path)
    os.chown(directory, 10001, 10001)
    Path(directory).chmod(0o700)
