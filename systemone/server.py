"""L1 (TZ-memdoor-5090-v2): thin decision server on :8002, path /v1/systemone.

Small model + LOGITS, never a chat fallback (Kev contract): the backend is an
OpenAI-compatible ``/v1/completions`` asked for ``logprobs``; probabilities come
from the first-token distribution. No logprobs in the response -> `unavailable`
(a degraded backend is a fact, not a coin flip). One in-flight turn (donor
LocalParallel=1): a second concurrent request is refused as `busy` rather than
queued. Successful verdicts cache for 1h (same question, same answer).

Run:  python -m systemone.server --port 8002 --backend http://127.0.0.1:8006
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contract import (LOCAL_PARALLEL, MAX_CHOICE_LEN, MAX_SCORE,
                       Decision, softmax_logprobs, validate_choice)

CACHE_TTL_S = 3600
_BACKEND_TIMEOUT_S = 5.0

_slot = threading.Semaphore(LOCAL_PARALLEL)
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_backend = "http://127.0.0.1:8006"
_model = "small"


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        return None


def _cache_put(key: str, blob: dict) -> None:
    with _cache_lock:
        if len(_cache) > 4096:
            _cache.clear()
        _cache[key] = (time.time(), blob)


_TOKEN_TEXT: dict[int, str] = {}
_TOKEN_TEXT_LOCK = threading.Lock()


def _token_text(token_id: int) -> str:
    """Detokenize one id via the backend /detokenize (cached per id).

    Some vLLM builds answer ``top_logprobs`` keyed ``token_id:NNN`` instead of
    token text; the contract speaks tokens, so ids are resolved — a failed
    lookup degrades to "" and simply drops that candidate."""
    with _TOKEN_TEXT_LOCK:
        if token_id in _TOKEN_TEXT:
            return _TOKEN_TEXT[token_id]
    try:
        req = urllib.request.Request(
            _backend.rstrip("/") + "/detokenize",
            data=json.dumps({"tokens": [token_id]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_BACKEND_TIMEOUT_S) as resp:
            text = json.loads(resp.read().decode()).get("prompt", "")
    except Exception:
        text = ""
    with _TOKEN_TEXT_LOCK:
        _TOKEN_TEXT[token_id] = text
    return text


def _complete(prompt: str) -> tuple[dict[str, float] | None, str]:
    """First-token logprob map {token: logprob}; (None, reason) on degradation."""
    body = {"model": _model, "prompt": prompt, "max_tokens": 1, "logprobs": 5,
            "temperature": 0}
    try:
        req = urllib.request.Request(
            _backend.rstrip("/") + "/v1/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_BACKEND_TIMEOUT_S) as resp:
            blob = json.loads(resp.read().decode())
        lp = blob["choices"][0]["logprobs"]
        top = lp["top_logprobs"][0]
    except Exception:
        return None, "model_down"
    if isinstance(top, dict):  # vllm shape {token: logprob}
        out: dict[str, float] = {}
        for t, v in top.items():
            name = str(t)
            if name.startswith("token_id:"):
                name = _token_text(int(name.split(":", 1)[1]))
            key = name.lower().strip()
            # base models emit layout tokens (\n) before the answer word: they
            # carry no decision signal and must not mask the candidates.
            if key:
                out[key] = max(out.get(key, -math.inf), float(v))
        return out, ""
    if isinstance(top, list):  # openai shape [{"token","logprob"}]
        try:
            return ({str(e["token"]).lower().strip(): float(e["logprob"])
                     for e in top if str(e["token"]).strip()}), ""
        except Exception:
            return None, "degraded_no_logprobs"
    return None, "degraded_no_logprobs"


def _decide_yesno(question: str) -> Decision:
    lp, reason = _complete(
        f"Answer with one word, yes or no.\nQ: {question}\nANSWER:")
    if lp is None:
        return Decision("yesno", None, None, "unavailable", reason)
    yes = max((v for t, v in lp.items() if t.startswith("y")), default=None)
    no = max((v for t, v in lp.items() if t.startswith("n")), default=None)
    if yes is None and no is None:
        return Decision("yesno", None, None, "unavailable", "unparsable")
    p = softmax_logprobs({"y": yes if yes is not None else -50.0,
                          "n": no if no is not None else -50.0})
    return Decision("yesno", p["y"] >= 0.5, round(p["y"], 4), "ok")


def _decide_choice(question: str, options: list[str]) -> Decision:
    listing = " | ".join(options)
    lp, reason = _complete(
        "Answer with exactly one of the options, verbatim.\n"
        f"Options: {listing}\nQ: {question}\nANSWER:")
    if lp is None:
        return Decision("choice", None, None, "unavailable", reason)
    cand: dict[str, float] = {}
    for opt in options:
        head = opt.lower().strip()
        match = [v for t, v in lp.items() if t and head.startswith(t)]
        if match:
            cand[opt] = max(match)
    if not cand:
        return Decision("choice", None, None, "unavailable", "unparsable")
    probs = softmax_logprobs(cand)
    winner = max(probs, key=lambda k: probs[k])
    return Decision("choice", winner, round(probs[winner], 4), "ok")


def _decide_score(question: str) -> Decision:
    lp, reason = _complete(
        f"Rate 0 to {MAX_SCORE}, one number only.\n{question}\nRATING:")
    if lp is None:
        return Decision("score", None, None, "unavailable", reason)
    num: dict[str, float] = {}
    for t, v in lp.items():
        head = t.strip().split()[0] if t.strip() else ""
        if head.isdigit() and int(head) <= MAX_SCORE:
            num[head] = max(num.get(head, -math.inf), v)
    if not num:
        return Decision("score", None, None, "unavailable", "unparsable")
    probs = softmax_logprobs(num)
    score = sum(float(k) * p for k, p in probs.items())
    return Decision("score", round(min(max(score, 0.0), float(MAX_SCORE)), 3),
                    None, "ok")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *a):  # quiet
        pass

    def _send(self, code: int, blob: dict) -> None:
        raw = json.dumps(blob).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/systemone":
            return self._send(404, {"status": "unavailable", "reason": "no_route"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode())
            kind = payload["kind"]
            question = str(payload["question"])
        except Exception:
            return self._send(400, {"status": "unavailable", "reason": "malformed"})
        if kind not in ("yesno", "choice", "score"):
            return self._send(400, {"status": "unavailable", "reason": "bad_kind"})
        options = payload.get("options") or []
        if kind == "choice":
            err = validate_choice(question, options)
            if err:
                return self._send(400, {"status": "unavailable", "reason": err})
        key = hashlib.sha256(json.dumps(
            [kind, question, sorted(options) if kind == "choice" else None],
            ensure_ascii=False).encode()).hexdigest()
        hit = _cache_get(key)
        if hit:
            return self._send(200, hit)
        if not _slot.acquire(blocking=False):
            return self._send(503, {"status": "unavailable", "reason": "busy"})
        try:
            if kind == "yesno":
                d = _decide_yesno(question)
            elif kind == "choice":
                d = _decide_choice(question, options)
            else:
                d = _decide_score(question)
        except Exception as exc:  # never 500 the caller: facts, not stacks
            d = Decision(kind, None, None, "unavailable", f"internal:{exc}")
        finally:
            _slot.release()
        if d.status != "ok":
            return self._send(503, {"status": "unavailable", "reason": d.reason})
        blob = {"status": "ok", "kind": kind, "value": d.value, "p": d.p}
        _cache_put(key, blob)
        self._send(200, blob)


def main() -> None:
    global _backend, _model
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--backend", default=_backend,
                    help="OpenAI-compatible base URL serving /v1/completions WITH logprobs")
    ap.add_argument("--model", default=_model)
    args = ap.parse_args()
    _backend, _model = args.backend, args.model
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
