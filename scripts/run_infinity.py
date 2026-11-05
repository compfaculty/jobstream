"""Launch the local runtime with only its required environment and credentials.

Invoke through `make infinity`, which loads .env using uv. Cargo build scripts do
not receive application secrets; the compiled runtime receives only its own keys.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "SDKROOT",
    "MACOSX_DEPLOYMENT_TARGET",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}
RUNTIME_KEYS = {
    "OPENAI_API_KEY",
    "INFINITY_SERVICE_TOKEN",
    "INFINITY_PROVIDER",
    "INFINITY_REASONING_EFFORT",
    "INFINITY_FAKE_DELAY_MS",
    "INFINITY_BIND",
    "INFINITY_DJANGO_URL",
    "INFINITY_DATABASE_URL",
    "RUST_LOG",
}


def main() -> None:
    provider = os.environ.get("INFINITY_PROVIDER", "openai")
    if provider not in {"openai", "fake"}:
        raise SystemExit("INFINITY_PROVIDER must be openai or fake.")
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Save OPENAI_API_KEY in .env before starting Infinity.")
    if len(os.environ.get("INFINITY_SERVICE_TOKEN", "")) < 32:
        raise SystemExit("INFINITY_SERVICE_TOKEN in .env must have at least 32 characters.")
    build_env = {key: value for key, value in os.environ.items() if key in SYSTEM_KEYS}
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            str(ROOT / "services/infinity-agent/Cargo.toml"),
        ],
        cwd=ROOT,
        env=build_env,
        check=True,
    )
    runtime_env = build_env | {
        key: value for key, value in os.environ.items() if key in RUNTIME_KEYS
    }
    binary = ROOT / "services/infinity-agent/target/debug/jobstream-infinity-agent"
    os.execve(binary, [str(binary)], runtime_env)


if __name__ == "__main__":
    main()
