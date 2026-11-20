"""The research engine's model pool (sabha/research/llm.py), offline: a fake hub lists two 'deep' servers,
one local (plain JSON) and one remote (another box: needs the token, answers as a stream). Checks:
calls spread over both, no server ever gets more calls at once than its concurrency, the bigger server
fills first, a remote that dies mid-run is benched and the calls finish on the local one.
Run: python tests/test_llm_pool.py
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket

TOKEN = "tok-pool"
LOCAL_PORT, REMOTE_PORT, HUB_PORT = 9101, 9102, 9103


class V6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def fake_vllm(name, need_token):
    st = {"now": 0, "peak": 0, "calls": 0, "lock": threading.Lock()}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if need_token and self.headers.get("Authorization") != "Bearer " + TOKEN:
                self.send_response(401); self.end_headers(); return
            with st["lock"]:
                st["now"] += 1; st["calls"] += 1; st["peak"] = max(st["peak"], st["now"])
            try:
                time.sleep(0.25)
                text = f"answer from {name}"
                if body.get("stream"):
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                    for piece in (text[:6], text[6:]):
                        self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': {'completion_tokens': 3}})}\n\ndata: [DONE]\n\n".encode())
                else:
                    out = json.dumps({"choices": [{"message": {"content": text}}], "usage": {"completion_tokens": 3}}).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(out)
            finally:
                with st["lock"]:
                    st["now"] -= 1
    return H, st


def main():
    LH, lst = fake_vllm("local", False)
    RH, rst = fake_vllm("remote", True)
    local = ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), LH)
    remote = V6(("::1", REMOTE_PORT), RH)
    models = [
        {"name": "local 9B (deep)", "served_name": "deep", "base_url": f"http://127.0.0.1:{LOCAL_PORT}/v1", "status": "ready",
         "context_window": 32768, "concurrency": 4},
        {"name": "A100 27B (a-deep)", "served_name": "deep", "base_url": f"http://[::1]:{REMOTE_PORT}/v1", "status": "ready",
         "context_window": 32768, "concurrency": 8, "api_key_env": "POOL_TEST_KEY"},
    ]

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.headers.get("Authorization") == "Bearer " + TOKEN
            self.send_response(200 if ok else 401); self.end_headers()
            if ok:
                self.wfile.write(json.dumps({"models": models}).encode())
    hub = ThreadingHTTPServer(("127.0.0.1", HUB_PORT), Hub)
    for s in (local, remote, hub):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    os.environ.update(SABHA_HUB_URL=f"http://127.0.0.1:{HUB_PORT}", SABHA_TOKEN=TOKEN, POOL_TEST_KEY=TOKEN, SABHA_LLM_WAIT_S="20")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from sabha.research import llm
    llm.trace.emit = lambda *a, **k: None

    fails = []

    def check(what, ok):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            fails.append(what)

    with ThreadPoolExecutor(24) as ex:
        out = list(ex.map(lambda i: llm.chat("deep", "sys", f"q{i}", max_tokens=10), range(40)))
    check("40 calls all answered", len(out) == 40 and all(o.startswith("answer from") for o in out))
    check(f"both servers used (local {lst['calls']}, remote {rst['calls']})", lst["calls"] > 0 and rst["calls"] > 0)
    check(f"remote streamed and authorised ({sum(o == 'answer from remote' for o in out)} answers)", any(o == "answer from remote" for o in out))
    check(f"concurrency respected (peak local {lst['peak']}<=4, remote {rst['peak']}<=8)", lst["peak"] <= 4 and rst["peak"] <= 8)
    check("bigger server took more calls", rst["calls"] > lst["calls"])

    # a single call, idle pool: goes to the biggest server
    before = rst["calls"]
    llm.chat("deep", "sys", "one")
    check("idle: one call goes to the biggest server", rst["calls"] == before + 1)

    # the remote box goes away mid-run: its calls fail, it is benched, everything finishes locally
    remote.shutdown(); remote.server_close()
    lc = lst["calls"]
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        out = list(ex.map(lambda i: llm.chat("deep", "sys", f"r{i}", max_tokens=10), range(12)))
    check(f"after the remote died: 12/12 answered locally in {time.time() - t0:.1f} s",
          all(o == "answer from local" for o in out) and lst["calls"] - lc == 12)
    st = {e["label"]: e for e in llm.pool_status()}
    check("the dead remote is benched", not st["A100 27B (a-deep)"]["ok"])
    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
