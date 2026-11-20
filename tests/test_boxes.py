"""Linked boxes end to end (sabha/boxes.py), on this machine: two real hubs and a fake model server.
  1. box B starts with SABHA_JOIN=<hub A's link>: it announces itself, A links it with no paste
  2. a model appears on B: A registers it as a remote model through B's /api/relay, and it answers through it
  3. B stops: after 3 missed polls A marks it down (box.updated event)
  4. B comes back on a NEW port (a new link): it re-announces, A moves its models to the new link, box is up
  5. unlink: A removes the box and its models
Run: python tests/test_boxes.py
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
TOK = "tok-boxes"
A_PORT, B_PORT, B_PORT2, FAKE = 8811, 8812, 8813, 8009  # FAKE: the relay serves 8000-8009 only
fails = []


def check(what, ok):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def call(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def wait(fn, secs, step=0.5):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def hub(port, data, env_extra):
    env = dict(os.environ, SABHA_TOKEN=TOK, SABHA_BOX_POLL_S="2", SABHA_BOXES_LOCAL="1", PYTHONUNBUFFERED="1", **env_extra)
    return subprocess.Popen([sys.executable, "-m", "sabha", "--port", str(port), "--data", data, "--tunnel", "none"],
                            cwd=ROOT, env=env, stdout=open(os.path.join(data + ".log"), "ab"), stderr=subprocess.STDOUT)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[{"id":"deep"}]}')

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "hello from box B"}}], "usage": {}}).encode())


def main():
    tmp = tempfile.mkdtemp(prefix="sabha_boxes_")
    fake = ThreadingHTTPServer(("127.0.0.1", FAKE), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    a = hub(A_PORT, os.path.join(tmp, "a"), {})
    b = None
    try:
        check("hub A up", wait(lambda: call(A_PORT, "GET", "/api/box"), 30))
        b = hub(B_PORT, os.path.join(tmp, "b"), {"SABHA_JOIN": f"http://127.0.0.1:{A_PORT}/#token={TOK}"})
        bid = (wait(lambda: call(B_PORT, "GET", "/api/box"), 30) or {}).get("id")
        boxes = wait(lambda: call(A_PORT, "GET", "/api/boxes")["boxes"], 30)
        check(f"1. B joined A by itself ({boxes and boxes[0]['label']}, via {boxes and boxes[0]['via']})",
              bool(boxes) and boxes[0]["id"] == bid and boxes[0]["via"] == "joined" and boxes[0]["status"] == "up")

        call(B_PORT, "POST", "/api/models", {"name": "Fake 9B (deep)", "kind": "chat", "served_name": "deep",
                                             "base_url": f"http://127.0.0.1:{FAKE}/v1"})
        def remote_model():
            ms = [m for m in call(A_PORT, "GET", "/api/state")["models"] if f"127.0.0.1:{B_PORT}/api/relay/{FAKE}/v1" in m["base_url"]]
            return ms[0] if ms and ms[0]["status"] == "ready" else None
        rm = wait(remote_model, 40)
        check(f"2. B's model registered on A through the relay and ready ({rm and rm['name']})", bool(rm))
        if rm:
            req = urllib.request.Request(rm["base_url"] + "/chat/completions", data=b'{"model":"deep","messages":[]}',
                                         headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
            out = json.loads(urllib.request.urlopen(req, timeout=10).read())
            check("2. a chat through A's registered URL reaches box B's model", out["choices"][0]["message"]["content"] == "hello from box B")

        b.terminate(); b.wait(10)
        down = wait(lambda: call(A_PORT, "GET", "/api/boxes")["boxes"][0]["status"] == "down", 30)
        check("3. B stopped: A marks it down", bool(down))

        b = hub(B_PORT2, os.path.join(tmp, "b"), {})  # same box (same data dir), new link; it remembers its hub
        def moved():
            x = call(A_PORT, "GET", "/api/boxes")["boxes"][0]
            return x if x["status"] == "up" and str(B_PORT2) in x["base"] else None
        mv = wait(moved, 40)
        check("4. B back on a new link: it re-announced, A moved it and it is up", bool(mv))
        ms = [m for m in call(A_PORT, "GET", "/api/state")["models"] if "/api/relay/" in m["base_url"]]
        check(f"4. its model now points at the new link ({len(ms)} model)", len(ms) == 1 and f":{B_PORT2}/" in ms[0]["base_url"])

        call(A_PORT, "DELETE", f"/api/boxes/{bid}")
        ms = [m for m in call(A_PORT, "GET", "/api/state")["models"] if "/api/relay/" in m["base_url"]]
        check("5. unlink removes the box and its models", not call(A_PORT, "GET", "/api/boxes")["boxes"] and not ms)
    finally:
        for p in (a, b):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        fake.shutdown()
    print("ALL PASSED" if not fails else f"{len(fails)} FAILED (logs in {tmp})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
