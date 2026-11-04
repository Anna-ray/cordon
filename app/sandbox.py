
"""Fail-closed execution of untrusted Python code in Docker."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")
MAX_CODE_BYTES = 64 * 1024
MAX_DATA_BYTES = 1024 * 1024


def docker_ok():
    """Check whether the Docker daemon is reachable."""
    docker = shutil.which("docker")
    if not docker:
        return False

    try:
        result = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _image_available(docker):
    """Require a pre-pulled image; never pull an image during execution."""
    try:
        result = subprocess.run(
            [docker, "image", "inspect", IMAGE],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _unavailable(reason):
    return {
        "ok": False,
        "stdout": "",
        "stderr": reason,
        "exit": -1,
        "killed": False,
        "ms": 0,
        "mode": "UNAVAILABLE",
    }


def run(code, data=None, timeout=10):
    """Execute code only inside a restricted Docker container.

    Docker unavailable, image missing, or invalid input:
    refuse execution. There is deliberately no local fallback.
    """
    if not isinstance(code, str):
        return _unavailable("Rejected: code must be a string.")

    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return _unavailable("Rejected: code exceeds size limit.")

    try:
        serialized = json.dumps(data if data is not None else {})
    except (TypeError, ValueError):
        return _unavailable("Rejected: input data is not JSON serializable.")

    if len(serialized.encode("utf-8")) > MAX_DATA_BYTES:
        return _unavailable("Rejected: input data exceeds size limit.")

    docker = shutil.which("docker")
    if not docker or not docker_ok():
        return _unavailable(
            "Docker daemon unavailable. Refusing unisolated execution."
        )

    if not _image_available(docker):
        return _unavailable(
            f"Sandbox image {IMAGE!r} is not available locally. "
            "Pull and verify it before running Cordon."
        )

    timeout = max(1, min(int(timeout), 30))
    directory = tempfile.mkdtemp(prefix="cordon_sandbox_")
    name = "cordon-" + uuid.uuid4().hex[:12]
    started = time.time()
    killed = False
    launched = False

    try:
        code_path = os.path.join(directory, "check.py")
        data_path = os.path.join(directory, "data.json")

        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)

        with open(data_path, "w", encoding="utf-8") as f:
            f.write(serialized)

        for path in (code_path, data_path):
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass

        command = [
            docker, "run",
            "--rm",
            "--pull=never",
            "--name", name,
            "--network=none",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--memory=128m",
            "--memory-swap=128m",
            "--cpus=0.5",
            "--pids-limit=64",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=65534:65534",
            "--mount", f"type=bind,source={directory},target=/work,readonly",
            "--workdir=/work",
            IMAGE,
            "python", "check.py",
        ]

        launched = True
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = (result.stdout or "")[-4000:]
            stderr = (result.stderr or "")[-4000:]
            exit_code = result.returncode

        except subprocess.TimeoutExpired:
            killed = True
            stdout = ""
            stderr = "Execution killed: timeout exceeded."
            exit_code = -9

        return {
            "ok": exit_code == 0 and not killed,
            "stdout": stdout,
            "stderr": stderr,
            "exit": exit_code,
            "killed": killed,
            "ms": int((time.time() - started) * 1000),
            "mode": "docker",
        }

    except (OSError, subprocess.SubprocessError) as exc:
        return {
            **_unavailable(f"Docker execution failed: {type(exc).__name__}: {exc}"),
            "ms": int((time.time() - started) * 1000),
        }

    finally:
        if launched:
            try:
                subprocess.run(
                    [docker, "rm", "-f", name],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass

        shutil.rmtree(directory, ignore_errors=True)
