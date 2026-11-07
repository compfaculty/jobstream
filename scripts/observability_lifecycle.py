"""Opt-in real Gunicorn and Celery prefork lifecycle tests, isolated from business queues."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def until(function, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = function()
            if value:
                return value
        except OSError, httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Lifecycle condition timed out")


def environment(tmp_path):
    directory = tmp_path / "metrics"
    directory.mkdir()
    values = {
        **os.environ,
        "DEBUG": "1",
        "PROMETHEUS_MULTIPROC_DIR": str(directory),
        "JOBSTREAM_METRICS_ENABLED": "1",
        "PYTHONPATH": str(ROOT),
        "JOBSTREAM_LOG_DIR": str(tmp_path / "logs"),
    }
    if sys.platform == "darwin":
        # Celery needs to rebuild its trace cache after macOS spawn/exec.
        values["FORKED_BY_MULTIPROCESSING"] = "1"
    return values


def scrape(env):
    code = (
        "from prometheus_client import CollectorRegistry,generate_latest,multiprocess; "
        "r=CollectorRegistry();multiprocess.MultiProcessCollector(r);print(generate_latest(r).decode())"
    )
    return subprocess.check_output([sys.executable, "-c", code], env=env, text=True)


def terminate(process):
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def verify_gunicorn_worker_replacement(tmp_path):
    env = environment(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "gunicorn.log").open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "scripts.observability_probe:application",
                "--config",
                "config/gunicorn.py",
                "--bind",
                f"127.0.0.1:{port}",
                "--workers",
                "2",
                "--no-control-socket",
            ],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            until(lambda: httpx.get(base, timeout=1).status_code == 200)
            seen = set()
            for _ in range(50):
                seen.add(int(httpx.get(base, timeout=2).text))
                if len(seen) == 2:
                    break
            assert len(seen) == 2
            os.kill(next(iter(seen)), signal.SIGKILL)

            def replaced():
                pid = int(httpx.get(base, timeout=2).text)
                return pid not in seen

            until(replaced)
            data = scrape(env)
            assert "jobstream_http_requests_total" in data
            assert "jobstream_http_in_progress 0.0" in data
        finally:
            terminate(process)


def verify_celery_prefork_retry_and_replacement(tmp_path):
    from celery import Celery
    from redis import Redis

    env = environment(tmp_path)
    queue = "observability-test-" + uuid4().hex
    env["PROBE_PID_FILE"] = str(tmp_path / "child.pid")
    broker = os.getenv("PROBE_BROKER", "redis://127.0.0.1:6379/15")
    env["PROBE_KEY_PREFIX"] = queue + ":"
    client = Celery("probe-client", broker=broker)
    client.conf.broker_transport_options = {"global_keyprefix": env["PROBE_KEY_PREFIX"]}
    with (tmp_path / "celery.log").open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "scripts.observability_probe",
                "worker",
                "--queues",
                queue,
                "--concurrency",
                "2",
                "--loglevel",
                "WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            until(lambda: list((tmp_path / "metrics").glob("*.db")))
            client.send_task("observability.probe", kwargs={"retry": True}, queue=queue)

            def completed():
                data = scrape(env)
                return (
                    'state="retry",task="other"} 1.0' in data
                    and 'state="success",task="other"} 1.0' in data
                )

            until(completed)
            assert 'jobstream_tasks_running{queue="other",task="other"} 0.0' in scrape(env)
            client.send_task("observability.probe", kwargs={"pause": 10}, queue=queue)
            until(lambda: (tmp_path / "child.pid").exists())
            child = int((tmp_path / "child.pid").read_text())
            os.kill(child, signal.SIGKILL)
            until(lambda: not (tmp_path / "metrics" / f"gauge_livesum_{child}.db").exists())
            client.send_task("observability.probe", queue=queue)
            until(lambda: 'state="success",task="other"} 2.0' in scrape(env))
            assert 'jobstream_tasks_running{queue="other",task="other"} 0.0' in scrape(env)
        finally:
            terminate(process)
            # Remove only this test's unique queue, never shared application queues.
            redis = Redis.from_url(broker)
            keys = list(redis.scan_iter(match=env["PROBE_KEY_PREFIX"] + "*", count=100))
            if keys:
                redis.delete(*keys)
            redis.close()


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(prefix="jobstream-lifecycle-") as directory:
        root = Path(directory)
        for name, verify in [
            ("gunicorn", verify_gunicorn_worker_replacement),
            ("celery", verify_celery_prefork_retry_and_replacement),
        ]:
            target = root / name
            target.mkdir()
            verify(target)
            print(name + ": retry/replacement/lifecycle checks passed")
