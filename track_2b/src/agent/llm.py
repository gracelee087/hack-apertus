"""Apertus client (OpenAI-compatible chat API) with a record/replay cache.

LLM_MODE:
  live   - call the endpoint, record every response to the cache
  replay - answer only from the cache (no network); a miss raises ReplayMiss
  auto   - live if LLM_API_KEY is set, else replay (default)

The cache key is the model name plus the exact messages and sampling params,
so a judge without an API key gets the same responses we measured.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import DATA, canonical, sha256

CACHE_PATH = Path(os.environ.get("LLM_CACHE_PATH", DATA / "cache" / "apertus_responses.jsonl"))
_lock = threading.Lock()
_cache = None


class ReplayMiss(RuntimeError):
    pass


DEFAULT_MODEL = "swiss-ai/apertus-v1.5-8b"


def model_name() -> str:
    """LLM_NAME if set; otherwise the model the recorded responses came from, so replay always matches."""
    if os.environ.get("LLM_NAME"):
        return os.environ["LLM_NAME"]
    recorded = {row["model"] for row in _load().values()}
    return recorded.pop() if len(recorded) == 1 else DEFAULT_MODEL


def mode() -> str:
    m = os.environ.get("LLM_MODE", "auto")
    if m == "auto":
        return "live" if os.environ.get("LLM_API_KEY") else "replay"
    return m


def _load():
    global _cache
    if _cache is None:
        _cache = {}
        if CACHE_PATH.exists():
            with open(CACHE_PATH, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        row = json.loads(line)
                        _cache[row["key"]] = row
    return _cache


def _post(messages, temperature, max_tokens):
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("LLM_BASE_URL is not set")
    body = json.dumps({"model": model_name(), "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "track2b-payment-agent/1.0",  # Public AI rejects requests without one
        "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}",
    })
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"] or ""
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 402, 403, 404):
                raise RuntimeError(f"Apertus endpoint error {e.code}: {e.read()[:300]!r}") from e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Apertus endpoint unreachable: {last}")


def chat(messages, temperature=0.0, max_tokens=600, tag="") -> str:
    key = sha256(canonical({"model": model_name(), "messages": messages,
                            "temperature": temperature, "max_tokens": max_tokens}))
    cache = _load()
    if key in cache:
        return cache[key]["response"]
    if mode() != "live":
        raise ReplayMiss(f"no recorded response for {tag or key[:12]}")
    t0 = time.time()
    text = _post(messages, temperature, max_tokens)
    row = {"key": key, "tag": tag, "model": model_name(), "latency_s": round(time.time() - t0, 2),
           "messages": messages, "response": text}
    with _lock:
        cache[key] = row
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return text


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences and prose)."""
    s = re.sub(r"```(?:json)?", "", text)
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
