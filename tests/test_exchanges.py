"""Conversations for Talk and Council (sabha/exchanges.py), end to end on this machine: a hub, a fake streaming
OpenAI server as the model, one Talk reply and one short council. Both must leave full exchanges readable through
GET /api/exchanges/{scope}/{id}: the system prompt, the input, the answer, which model, which agent.
Run: python tests/test_exchanges.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TOK, PORT, FAKE = "tok-x", 8821, 9131
fails = []


def check(what, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + what + (f"  -- {extra}" if not ok and extra else ""), flush=True)
    if not ok:
        fails.append(what)


def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"null")


def wait(fn, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.5)
    return None


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[{"id":"fake"}]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sysmsg = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        text = f"I am answering as told: {sysmsg[:30]}"
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for piece in (text[:10], text[10:]):
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': {'completion_tokens': 9, 'prompt_tokens': 120}})}\n\ndata: [DONE]\n\n".encode())


def main():
    tmp = tempfile.mkdtemp(prefix="sabha_x_")
    fake = ThreadingHTTPServer(("127.0.0.1", FAKE), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    hub = subprocess.Popen([sys.executable, "-m", "sabha", "--port", str(PORT), "--data", os.path.join(tmp, "d"), "--tunnel", "none"],
                           cwd=ROOT, env=dict(os.environ, SABHA_TOKEN=TOK), stdout=open(os.path.join(tmp, "hub.log"), "wb"),
                           stderr=subprocess.STDOUT)
    try:
        wait(lambda: call("GET", "/api/state"), 30)
        m = call("POST", "/api/models", {"name": "Fake 9B", "kind": "chat", "base_url": f"http://127.0.0.1:{FAKE}/v1", "served_name": "fake"})
        wait(lambda: [x for x in call("GET", "/api/state")["models"] if x["id"] == m["id"] and x["status"] == "ready"], 20)
        agents = [call("POST", "/api/agents", {"name": n, "role": "member", "model_id": m["id"], "harness": "dsh"}) for n in ("Ada", "Bo")]

        th = call("POST", "/api/threads", {"agent_id": agents[0]["id"]})
        call("POST", f"/api/threads/{th['id']}/messages", {"content": "What should a council of small models do first?"})
        xs = wait(lambda: call("GET", f"/api/exchanges/talk/{th['id']}")["rows"], 30)
        check("talk: the reply left an exchange", bool(xs))
        if xs:
            f = call("GET", f"/api/exchanges/talk/{th['id']}/{xs[0]['n']}")
            check("talk: full exchange has system, input, answer, model, agent",
                  f["system"] and "council of small models" in f["input"] and f["output"].startswith("I am answering")
                  and f["endpoint"] == "Fake 9B" and f["track"] == "Ada" and f["tok"] == 9, json.dumps(f)[:300])

        method = "We route each task between a small and a large model by a learned router that predicts success. " * 12
        results = "Routing halves the cost at equal accuracy on three agent benchmarks, with no loss on hard tasks. " * 12
        c = call("POST", "/api/councils", {"text": f"# A tiny paper\n\n## Method\n{method}\n\n## Results\n{results}\n",
                                            "agent_ids": [a["id"] for a in agents], "rounds": 1})
        done = wait(lambda: call("GET", f"/api/councils/{c['id']}")["status"] in ("done", "failed", "stopped"), 60)
        lst = call("GET", f"/api/exchanges/council/{c['id']}")
        steps = {r["step"] for r in lst["rows"]}
        check(f"council: ended ({bool(done)}) and every turn recorded ({lst['total']} exchanges, steps {sorted(steps)})",
              bool(done) and lst["total"] >= 3 and {"brief", "verdict"} <= steps,
              json.dumps({k: v for k, v in call("GET", f"/api/councils/{c['id']}").items() if k in ("status", "error", "note")}))
        check("council: both members appear as speakers", {"Ada", "Bo"} <= set(lst["tracks"]), str(lst["tracks"]))
        try:
            call("GET", "/api/exchanges/nope/x")
            check("bad scope refused", False)
        except urllib.error.HTTPError as e:
            check("bad scope refused", e.code == 404)
    finally:
        hub.terminate()
        try:
            hub.wait(10)
        except subprocess.TimeoutExpired:
            hub.kill()
        fake.shutdown()
    print("ALL PASSED" if not fails else f"{len(fails)} FAILED (logs in {tmp})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
