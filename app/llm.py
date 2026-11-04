import json, os, re, httpx

BASE = os.getenv("VULTR_INFERENCE_URL", "https://api.vultrinference.com/v1")
_H = lambda: {"Authorization": "Bearer " + os.environ["VULTR_INFERENCE_KEY"]}
_model = os.getenv("VULTR_MODEL", "")

def models():
    return [m["id"] for m in httpx.get(f"{BASE}/models", headers=_H(), timeout=30).json()["data"]]

def chat(messages):
    global _model
    _model = _model or models()[0]  # set VULTR_MODEL to pin a model
    r = httpx.post(f"{BASE}/chat/completions", headers=_H(), timeout=120,
                   json={"model": _model, "messages": messages, "temperature": 0.1})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def extract_json(t):
    return json.loads(re.search(r"(\{.*\}|\[.*\])", t, re.S).group(1))

def extract_code(t):
    m = re.search(r"```(?:python)?\n(.*?)```", t, re.S)
    return m.group(1) if m else t
