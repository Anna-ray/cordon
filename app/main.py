import json, pathlib
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from . import sandbox, cordon

ROOT = pathlib.Path(__file__).parent.parent
CASES = {c["id"]: c for c in json.load(open(ROOT / "eval/cases.json"))}
RUNS = {}
app = FastAPI(title="Cordon")

@app.get("/api/health")
def health(): return {"sandbox": "docker" if sandbox.docker_ok() else "DEGRADED (no Docker)"}

@app.get("/api/cases")
def cases(): return [{"id": c["id"], "name": c["name"]} for c in CASES.values()]

@app.post("/api/investigate/{cid}")
def investigate(cid: str):
    if cid not in CASES: raise HTTPException(404)
    r = cordon.investigate(CASES[cid]); RUNS[r["run_id"]] = r; return r

@app.post("/api/approve/{rid}")
def approve(rid: str):
    r = RUNS.get(rid)
    if not r or not r["action"]: raise HTTPException(404)
    r["action"]["status"] = "approved_and_executed"  # simulated: records an audit entry only
    cordon.audit("action_executed", run=rid, action=r["action"]["type"], approved_by="human")
    return r["action"]

@app.get("/api/containment")
def containment(): return sandbox.containment_suite()

app.mount("/", StaticFiles(directory=ROOT / "static", html=True))
