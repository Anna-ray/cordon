"""Offline logic test with a scripted fake LLM (no Vultr key needed). Not a substitute for the live eval."""
import json, pathlib
from app import cordon, sandbox
case = {c["id"]: c for c in json.load(open("eval/cases.json"))}["borealis-mismatch"]
CODE = {"revenue": "import json;d=json.load(open('/work/data.json'))['fields'] if False else json.load(open('data.json'))['fields']\ns=sum(d['quarters']);print(json.dumps({'passed':s==d['revenue_stated'],'observed':f'sum={s}','detail':'quarters vs stated'}))",
        "founded": "import json;d=json.load(open('data.json'))['fields']\nprint(json.dumps({'passed':d['registered'][:4]==str(d['founded_claimed']),'observed':d['registered'],'detail':'registration year'}))"}
def fake(msgs):
    s, u = msgs[0]["content"], msgs[1]["content"]
    if s.startswith("You are the Orchestrator"):
        d = json.loads(u); p = [c["id"] for c in d["claims"] if c["status"] == "pending"]
        return json.dumps({"action": "verify", "targets": p, "why": "pending"} if p else {"action": "conclude" if d["challenged"] else "challenge", "targets": [], "why": "x"})
    if s.startswith("Extract"): return json.dumps([{"id":"c1","claim":"revenue is 12M","severity":"high","doc":"profile.txt","quote":"annual revenue 12M"},{"id":"c2","claim":"founded 2012","severity":"medium","doc":"profile.txt","quote":"operating since 2012"},{"id":"c3","claim":"ceo is X","severity":"medium","doc":"profile.txt","quote":"fabricated quote"}])
    if s.startswith("Write"): return "```python\n" + CODE["revenue" if "revenue" in u.split("\n")[0] else "founded"] + "\n```"
    return json.dumps({"contradictions":[{"between":["c1","c2"],"why":"x"}],"extra_claims":[]})
r = cordon.investigate(case, chat=fake, run=lambda c, d, **k: sandbox.run(c, d, **k))
print(r["verdict"], r["evidence_coverage"], [(x["id"], x["passed"], x["observed"]) for x in r["ledger"]])
assert r["verdict"] == "REJECT"
assert [x["anchored"] for x in r["ledger"]] == [True, True, False]
print([t["agent"] for t in r["trace"]])
assert [o["action"] for o in r["orchestration"]] == ["verify", "challenge", "conclude"], r["orchestration"]
print([(x["attack"], x["contained"]) for x in sandbox.containment_suite()])
