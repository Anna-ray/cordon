"""Cordon (sandbox-verified) vs single-agent baseline (one LLM call, no execution). Needs VULTR_INFERENCE_KEY."""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from app import llm, cordon

cases = json.load(open(pathlib.Path(__file__).parent / "cases.json"))
BASE = 'Assess this counterparty from the data. Reply JSON {"verdict":"CLEAR|ESCALATE|REJECT"}. ' + cordon.RULES
hit_v = hit_b = 0
for c in cases:
    v = cordon.investigate(c)["verdict"]
    b = llm.extract_json(llm.chat([{"role": "system", "content": BASE}, {"role": "user", "content": json.dumps(c["documents"]) + json.dumps(c["fields"])}]))["verdict"]
    hit_v += v == c["expected"]; hit_b += b == c["expected"]
    print(f'{c["id"]:20} expected={c["expected"]:8} verdict={v:8} baseline={b}')
print(f"Cordon {hit_v}/{len(cases)}  |  baseline {hit_b}/{len(cases)}")
