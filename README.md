# Cordon: counterparty due diligence with contained, verifiable execution
Vultr Agent Rush, Challenge 1 (Blast Radius Zero).

**Use case.** Given a counterparty's documents and fields, Cordon extracts checkable claims, then a Challenger agent
has the model write Python that verifies each claim. That code runs in a throwaway sandbox and the real output goes
into an evidence ledger. Failed or unverifiable claims produce REJECT/ESCALATE. The proposed action needs human approval.

## Agents (each is a distinct code path with its own permissions)
- **Orchestrator**: owns the shared investigation state and decides the next action each step (verify in parallel, challenge, deepen with an independent second check, conclude). Deterministic guardrails override it: claims are never left unverified, the Challenger always runs before a conclusion, and a step budget guarantees termination. State is persisted to `runs/<id>.json`.
- **Provenant**: extracts claims and anchors each to a verbatim quote in a named document; unanchored claims force ESCALATE.
- **Assay**: writes verification code and runs it in the sandbox (the only agent that executes code).
- **Challenger**: finds contradictions between ledger items and adds extra checks.
- **Proofline**: seals the evidence ledger, computes coverage, writes the audit trail (deterministic, no LLM).
- **Warrant**: issues the decision and gates every action behind human approval.

## Architecture on Vultr
- **Vultr Cloud Compute VM**: control plane (FastAPI), planning, ledger, audit log, Docker daemon for sandboxes.
- **Vultr Serverless Inference** (`https://api.vultrinference.com/v1`, OpenAI-compatible): all agent LLM calls (`app/llm.py`).
- **Sandbox** (`app/sandbox.py`): `docker run --rm --network none --read-only --cap-drop ALL --memory 128m --cpus 0.5 --pids-limit 64`,
  non-root, input mounted read-only, 10 s kill timeout, container destroyed after every task, no API keys inside.
- Untrusted document text is passed as data. Prompt-injection patterns are logged to `audit.jsonl`.

## Run on the VM
```
sudo apt install -y docker.io && sudo docker pull python:3.12-slim   # run the app as a user in the docker group
pip install -r requirements.txt
export VULTR_INFERENCE_KEY=...   # optional: VULTR_MODEL=<id from /v1/models>
uvicorn app.main:app --host 0.0.0.0 --port 80
python eval/run_eval.py          # Cordon vs single-agent baseline
```
## Containment demo
UI button "Run containment red-team" runs six hostile payloads (secret read, network exfiltration, write outside the
workdir, memory bomb, fork bomb, infinite loop) and reports CONTAINED/BREACH. Case `cedar-injection` has a hostile document.

## Limits
Approved actions are simulated (audit-log entry). Cases are synthetic. Without Docker the runner falls back to a
non-isolated local mode and the UI says so; never demo in that mode.
