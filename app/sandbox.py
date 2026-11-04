"""Contained execution. Docker mode: no network, read-only FS, no secrets, caps dropped,
mem/cpu/pid/time limits, container destroyed after every run."""
import json, os, resource, shutil, subprocess, tempfile, time

IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")

def docker_ok():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False

def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (512 << 20,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))

def run(code, data=None, timeout=10):
    d = tempfile.mkdtemp(prefix="vsb_"); os.chmod(d, 0o755)
    for fn, body in (("check.py", code), ("data.json", json.dumps(data or {}))):
        open(f"{d}/{fn}", "w").write(body); os.chmod(f"{d}/{fn}", 0o644)
    name = "vsb-" + os.urandom(4).hex()
    if docker_ok():
        mode, kw = "docker", {}
        cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
               "--tmpfs", "/tmp:size=16m", "--memory", "128m", "--memory-swap", "128m",
               "--cpus", "0.5", "--pids-limit", "64", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", "65534:65534",
               "-v", f"{d}:/work:ro", "-w", "/work", IMAGE, "python", "check.py"]
    else:
        mode, kw = "DEGRADED-local (no Docker: NOT isolated)", dict(cwd=d, env={}, preexec_fn=_limits)
        cmd = ["python3", "check.py"]
    t0, killed = time.time(), False
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        out, err, rc = p.stdout[-4000:], p.stderr[-4000:], p.returncode
    except subprocess.TimeoutExpired:
        killed, out, err, rc = True, "", "killed: timeout", -9
        if mode == "docker":
            subprocess.run(["docker", "kill", name], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": rc == 0, "stdout": out, "stderr": err, "exit": rc, "killed": killed,
            "ms": int((time.time() - t0) * 1000), "mode": mode}

PAYLOADS = {
 "read_secrets": "import os\nl=[k for k in os.environ if any(s in k.upper() for s in ('KEY','TOKEN','SECRET'))]\nprint('BREACH' if l else 'BLOCKED: no secrets inside sandbox')",
 "network_exfil": "import socket\ntry:\n    socket.create_connection(('1.1.1.1',80),2);print('BREACH')\nexcept Exception as e:\n    print('BLOCKED:',type(e).__name__)",
 "write_outside": "try:\n    open('/etc/pwned','w').write('x');print('BREACH')\nexcept Exception as e:\n    print('BLOCKED:',type(e).__name__)",
 "memory_bomb": "x=bytearray(1<<30);print('BREACH')",
 "fork_bomb": "import os,time\nn=0\ntry:\n    for _ in range(120):\n        if os.fork()==0:\n            time.sleep(4);os._exit(0)\n        n+=1\n    print('BREACH')\nexcept Exception:\n    print('BLOCKED after',n,'forks')",
 "infinite_loop": "while True: pass",
}

def containment_suite():
    rows = []
    for k, c in PAYLOADS.items():
        r = run(c, timeout=6)
        rows.append({"attack": k, "contained": "BREACH" not in r["stdout"], "killed": r["killed"],
                     "exit": r["exit"], "evidence": (r["stdout"].strip() or r["stderr"].strip())[-120:], "mode": r["mode"]})
    return rows
