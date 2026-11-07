"""Supervise per-container exposition and the application, preserving exit status."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    exporter = None
    if os.getenv("JOBSTREAM_METRICS_ENABLED") == "1":
        try:
            directory = Path(os.environ["PROMETHEUS_MULTIPROC_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            for file in directory.glob("*.db"):
                file.unlink()
        except OSError:
            os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            os.environ["JOBSTREAM_METRICS_ENABLED"] = "0"
        else:
            exporter = subprocess.Popen([sys.executable, "-m", "jobs.observability.exporter"])  # noqa: S603
    application = subprocess.Popen(sys.argv[1:])  # noqa: S603
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in (application, exporter):
            if child is None:
                continue
            if child.poll() is None:
                child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    restarts = 0
    while application.poll() is None:
        if not stopping and exporter is not None and exporter.poll() is not None:
            # Exposition failure must never terminate a business process.
            if restarts < 3:
                exporter = subprocess.Popen([sys.executable, "-m", "jobs.observability.exporter"])  # noqa: S603
                restarts += 1
            else:
                exporter = None
        time.sleep(0.5)
    if exporter is not None:
        exporter.terminate()
        try:
            exporter.wait(timeout=5)
        except subprocess.TimeoutExpired:
            exporter.kill()
            exporter.wait()
    code = application.returncode
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
