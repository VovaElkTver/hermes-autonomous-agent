"""L1 (TZ-memdoor-5090-v2): donor client-contract acceptance — yesno/choice/
score vectors over HTTP, timeout/dead-backend -> unavailable (NEVER a negative),
LocalParallel=1 busy refusal, 1h cache."""
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from systemone.client import decide_choice, decide_score, decide_yesno
from systemone.contract import Decision

HITS = {"n": 0}
SLOW = {"on": False}


class StubBackend(BaseHTTPRequestHandler):
    def log_message(self, format, *a):
        pass

    def do_POST(self):
        HITS["n"] += 1
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n).decode())
        if SLOW["on"]:
            time.sleep(1.0)
        prompt = payload["prompt"]
        if "yes or no" in prompt:
            top = {" yes": -0.05, " no": -3.0}
        elif "Options:" in prompt:
            top = {" red": -0.2, " blue": -1.6}
        else:  # rating
            top = {" 7": -0.3, " 4": -2.0}
        body = {"choices": [{"text": " yes", "logprobs":
                             {"top_logprobs": [top]}}]}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def stack(request):
    bport, oport = free_port(), free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", bport), StubBackend)
    t = threading.Thread(target=stub.serve_forever, daemon=True)
    t.start()
    import os
    proc = subprocess.Popen(
        [sys.executable, "-m", "systemone.server", "--port", str(oport),
         "--backend", f"http://127.0.0.1:{bport}"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{oport}/nope", timeout=0.5)
            break
        except urllib.error.HTTPError:
            break  # 404 = alive
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{oport}", f"http://127.0.0.1:{bport}"
    proc.terminate()
    stub.shutdown()


def _url(request, port):
    return port


def test_yesno_vector(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    d = decide_yesno("is the sky up?")
    assert d.status == "ok" and d.value is True and d.p > 0.9


def test_choice_vector(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    d = decide_choice("pick a color", ["red", "blue"])
    assert d.status == "ok" and d.value == "red" and 0 < d.p <= 1


def test_choice_rejects_long_option(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    d = decide_choice("q", ["a" * 300, "b"])
    assert d.status == "unavailable"  # client-side contract refusal, no HTTP


def test_score_vector(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    d = decide_score("how risky?")
    assert d.status == "ok" and 4 <= d.value <= 7


def test_dead_backend_is_unavailable_not_no(stack, monkeypatch):
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", "http://127.0.0.1:1")  # nothing there
    d = decide_yesno("q")
    assert d.status == "unavailable" and d.value is None
    with pytest.raises(TypeError):
        bool(d)  # truthiness forbidden: an unavailable verdict must not read as 'no'


def test_backend_down_is_unavailable_503():
    """Kev contract core: a degraded model answers 503 'unavailable' — the
    caller must never read it as a 'no'."""
    import os
    oport, dead = free_port(), free_port()
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "systemone.server", "--port", str(oport),
         "--backend", f"http://127.0.0.1:{dead}"],
        cwd=root, env={**os.environ, "PYTHONPATH": root},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{oport}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/nope", timeout=0.5)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.1)
        req = urllib.request.Request(base + "/v1/systemone", data=json.dumps(
            {"kind": "yesno", "question": "q"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            blob = json.loads(exc.read().decode())
            assert blob["status"] == "unavailable"
            assert blob["reason"] == "model_down"
    finally:
        proc.terminate()


def test_busy_parallel_one(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    SLOW["on"] = True
    try:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(2) as ex:
            f1 = ex.submit(decide_yesno, "slow one")
            time.sleep(0.05)
            f2 = ex.submit(decide_yesno, "slow two")
            a, b = f1.result(10), f2.result(10)
        assert {a.status, b.status} == {"ok", "unavailable"} or \
            (a.status == "ok" and b.reason == "busy") or \
            (b.status == "ok" and a.reason == "busy")
    finally:
        SLOW["on"] = False


def test_cache_hit_no_second_backend_call(stack, monkeypatch):
    base, _ = stack
    monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
    q = "cache me " + str(time.time())
    before = HITS["n"]
    r1 = _raw_yesno(base, q)
    r2 = _raw_yesno(base, q)
    assert r1 == r2 and r1["status"] == "ok"
    assert HITS["n"] - before == 1  # second verdict came from the cache


def _raw_yesno(base, q):
    req = urllib.request.Request(base + "/v1/systemone", data=json.dumps(
        {"kind": "yesno", "question": q}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())



class TokenIdBackend(BaseHTTPRequestHandler):
    """Real-shape vLLM (this fleet): top_logprobs keyed token_id:NNN, top
    candidate a layout newline, the yes/no words behind it."""
    IDS = {7179: " Yes", 21114: " No", 271: "\n\n", 25: "I", 279: " the"}

    def log_message(self, format, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n).decode())
        if self.path == "/detokenize":
            raw = json.dumps({"prompt": "".join(
                self.IDS.get(int(t), "?") for t in payload["tokens"])}).encode()
        else:
            top = {"token_id:271": -0.02, "token_id:7179": -2.5,
                   "token_id:21114": -4.0, "token_id:25": -4.5,
                   "token_id:279": -5.0}
            raw = json.dumps({"choices": [{"text": "\n\n", "logprobs":
                                  {"top_logprobs": [top]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_token_id_shape_with_newline_top_is_parsable(monkeypatch):
    """Port defect claim 27.11 (rtx5000 E2E): keys token_id:NNN and a \n\n
    first candidate used to produce 'unparsable' — they must resolve via
    /detokenize and be filtered as non-signal."""
    import os
    bport, oport = free_port(), free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", bport), TokenIdBackend)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "systemone.server", "--port", str(oport),
         "--backend", f"http://127.0.0.1:{bport}"],
        cwd=root, env={**os.environ, "PYTHONPATH": root},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{oport}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/nope", timeout=0.5)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.1)
        monkeypatch.setenv("HERMES_SYSTEMONE_URL", base)
        d = decide_yesno("q")
        assert d.status == "ok", (d.status, d.reason)
        assert d.value is True and 0 < d.p < 1  # yes outranks no, \n filtered
    finally:
        proc.terminate()
        stub.shutdown()
