"""
Verification tests for AsyncClient (v1.2.0+).

Runs a local echo server so no external network is needed.

Run with:
    maturin develop
    python tests/verify_async_client.py
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests as req

from cello import App, AsyncClient

# ---------------------------------------------------------------------------
# Local echo server (replaces httpbin.org)
# ---------------------------------------------------------------------------

ECHO_PORT = 18080


class EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress noise

    def _respond(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self._respond({
            "method": "GET",
            "path": self.path,
            "headers": dict(self.headers),
        })

    def do_POST(self):
        body = self._read_body()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        self._respond({
            "method": "POST",
            "path": self.path,
            "json": parsed,
            "data": body.decode(errors="replace"),
            "headers": dict(self.headers),
        })

    def do_PUT(self):
        body = self._read_body()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        self._respond({
            "method": "PUT",
            "json": parsed,
            "headers": dict(self.headers),
        })

    def do_PATCH(self):
        body = self._read_body()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        self._respond({
            "method": "PATCH",
            "json": parsed,
            "headers": dict(self.headers),
        })

    def do_DELETE(self):
        self._respond({"method": "DELETE", "headers": dict(self.headers)})


def start_echo_server():
    server = HTTPServer(("127.0.0.1", ECHO_PORT), EchoHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)


ECHO = f"http://127.0.0.1:{ECHO_PORT}"

# ---------------------------------------------------------------------------
# Test app
# ---------------------------------------------------------------------------

app = App()
results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    msg = f"[{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    results.append(passed)


client = AsyncClient(timeout=5.0)


@app.get("/test/get")
async def test_get(request):
    resp = await client.get(f"{ECHO}/get")
    check("GET status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("GET json() returns dict", isinstance(data, dict), str(type(data)))
    check("GET json method echoed", data.get("method") == "GET")
    check("GET text is str", isinstance(resp.text, str))
    check("GET content is bytes", isinstance(resp.content, bytes))
    return {"ok": True}


@app.get("/test/post-json")
async def test_post_json(request):
    resp = await client.post(f"{ECHO}/post", json={"key": "value"})
    check("POST json status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("POST json body echoed", data.get("json") == {"key": "value"}, str(data.get("json")))
    return {"ok": True}


@app.get("/test/post-bytes")
async def test_post_bytes(request):
    resp = await client.post(
        f"{ECHO}/post",
        content=b"raw bytes payload",
        headers={"Content-Type": "application/octet-stream"},
    )
    check("POST bytes status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("POST bytes data echoed", "raw bytes payload" in data.get("data", ""), str(data.get("data")))
    return {"ok": True}


@app.get("/test/put")
async def test_put(request):
    resp = await client.put(f"{ECHO}/put", json={"updated": True})
    check("PUT status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("PUT json body echoed", data.get("json") == {"updated": True}, str(data.get("json")))
    return {"ok": True}


@app.get("/test/patch")
async def test_patch(request):
    resp = await client.patch(f"{ECHO}/patch", json={"patched": True})
    check("PATCH status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("PATCH json body echoed", data.get("json") == {"patched": True}, str(data.get("json")))
    return {"ok": True}


@app.get("/test/delete")
async def test_delete(request):
    resp = await client.delete(f"{ECHO}/delete")
    check("DELETE status == 200", resp.status == 200, f"got {resp.status}")
    data = resp.json()
    check("DELETE method echoed", data.get("method") == "DELETE")
    return {"ok": True}


@app.get("/test/headers")
async def test_headers(request):
    resp = await client.get(
        f"{ECHO}/headers",
        headers={"X-Custom-Header": "cello-test"},
    )
    check("Custom header status == 200", resp.status == 200)
    data = resp.json()
    sent = data.get("headers", {})
    # header names may be title-cased by the stdlib server
    found = sent.get("X-Custom-Header") or sent.get("x-custom-header")
    check("Custom header forwarded", found == "cello-test", f"headers={sent}")
    return {"ok": True}


@app.get("/test/context-manager")
async def test_context_manager(request):
    async with AsyncClient(timeout=5.0) as c:
        resp = await c.get(f"{ECHO}/get")
        check("Context manager GET status == 200", resp.status == 200, f"got {resp.status}")
        data = resp.json()
        check("Context manager json() works", isinstance(data, dict))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all():
    start_echo_server()

    def start_cello():
        app.run(port=8765, workers=1)

    t = threading.Thread(target=start_cello, daemon=True)
    t.start()
    time.sleep(1.5)

    base = "http://127.0.0.1:8765"
    endpoints = [
        "/test/get",
        "/test/post-json",
        "/test/post-bytes",
        "/test/put",
        "/test/patch",
        "/test/delete",
        "/test/headers",
        "/test/context-manager",
    ]

    for ep in endpoints:
        try:
            req.get(f"{base}{ep}", timeout=10)
        except Exception as e:
            print(f"[FAIL] Request to {ep} failed: {e}")
            results.append(False)

    passed = sum(results)
    total = len(results)
    print(f"\n{'='*40}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
