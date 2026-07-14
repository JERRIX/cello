"""
Regression tests for bug fixes shipped in Cello v1.3.0.

These are integration tests that exercise behaviour observable over HTTP. The
security/parsing fixes that live entirely inside Rust (CSRF origin validation,
SSE field-injection, Range underflow, skip-path prefix matching) are covered by
`#[cfg(test)]` unit tests in the corresponding `src/` modules — run them with
`cargo test`.

Run with:
    maturin develop
    pytest tests/test_v130_fixes.py -v
"""

import threading
import time

import pytest
import requests

PORT = 18130
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server():
    """Start a test server exercising the v1.3.0 fixes."""
    from cello import App

    app = App()

    @app.get("/bigint")
    def bigint(req):
        # 2**63 == i64::MAX + 1 (fits in u64). Must NOT be downgraded to a float.
        return {
            "u64_max": 18446744073709551615,  # 2**64 - 1
            "over_i64": 9223372036854775808,  # 2**63
            "normal": 42,
            "negative": -100,
        }

    @app.delete("/resource")
    def delete_with_body(req):
        # DELETE bodies are valid (RFC 7231) and must be readable.
        try:
            data = req.json()
        except Exception:
            data = None
        return {"body": data}

    @app.get("/echo-query")
    def echo_query(req):
        # `req.query` should reflect '+'-as-space decoding for keys AND values.
        return {"query": dict(req.query)}

    def run_server():
        try:
            app.run(host="127.0.0.1", port=PORT)
        except Exception:
            pass

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.5)
    yield BASE


@pytest.mark.integration
def test_large_integers_are_not_corrupted(server):
    """C7: Python ints beyond i64 must serialize exactly, not as lossy floats."""
    data = requests.get(f"{server}/bigint").json()
    assert data["u64_max"] == 18446744073709551615
    assert isinstance(data["u64_max"], int)
    assert data["over_i64"] == 9223372036854775808
    assert isinstance(data["over_i64"], int)
    assert data["normal"] == 42
    assert data["negative"] == -100


@pytest.mark.integration
def test_delete_request_body_is_read(server):
    """C11: a body sent with DELETE must reach the handler."""
    resp = requests.delete(f"{server}/resource", json={"reason": "cleanup", "id": 7})
    assert resp.status_code == 200
    assert resp.json()["body"] == {"reason": "cleanup", "id": 7}


@pytest.mark.integration
def test_query_plus_is_decoded_for_keys_and_values(server):
    """C12: '+' decodes to space in both query keys and values."""
    # Send a raw query string so the '+' is preserved on the wire.
    resp = requests.get(f"{server}/echo-query?a+b=c+d")
    assert resp.status_code == 200
    query = resp.json()["query"]
    assert query.get("a b") == "c d"


# =============================================================================
# Batch 2: request size limits + timeouts
# =============================================================================

LIMITS_PORT = 18131
LIMITS_BASE = f"http://127.0.0.1:{LIMITS_PORT}"


@pytest.fixture(scope="module")
def limited_server():
    """Server with a 1 KB body cap and a 1 s handler timeout."""
    from cello import App, LimitsConfig, TimeoutConfig

    app = App()
    app.set_limits(LimitsConfig(max_body_size=1024))
    app.set_timeouts(TimeoutConfig(handler=1))

    @app.post("/upload")
    def upload(req):
        return {"ok": True}

    @app.get("/slow")
    async def slow(req):
        import asyncio

        await asyncio.sleep(3)
        return {"ok": True}

    def run_server():
        try:
            app.run(host="127.0.0.1", port=LIMITS_PORT)
        except Exception:
            pass

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.5)
    yield LIMITS_BASE


@pytest.mark.integration
def test_body_within_limit_is_accepted(limited_server):
    """C1: a body under the cap is processed normally."""
    resp = requests.post(f"{limited_server}/upload", data=b"x" * 100)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.integration
def test_oversized_body_is_rejected(limited_server):
    """C1: a body over the cap is rejected with 413 (not buffered/OOM)."""
    resp = requests.post(f"{limited_server}/upload", data=b"x" * 5000)
    assert resp.status_code == 413


@pytest.mark.integration
def test_handler_timeout_returns_504(limited_server):
    """C5: an async handler exceeding handler_timeout returns 504."""
    resp = requests.get(f"{limited_server}/slow", timeout=10)
    assert resp.status_code == 504


# =============================================================================
# Batch 3: persistent asyncio loop (async handlers + lifecycle hooks)
# =============================================================================

ASYNC_PORT = 18132
ASYNC_BASE = f"http://127.0.0.1:{ASYNC_PORT}"


@pytest.fixture(scope="module")
def async_server():
    """Server exercising loop-bound resources and async lifecycle hooks."""
    from cello import App

    app = App()
    state = {"lock": None, "counter": 0, "started": False}

    @app.on_event("startup")
    async def on_start():
        import asyncio

        await asyncio.sleep(0)
        state["started"] = True

    @app.get("/started")
    def started(req):
        return {"started": state["started"]}

    @app.get("/loopbound")
    async def loopbound(req):
        import asyncio

        # An asyncio.Lock is bound to the loop that created it. With a fresh
        # asyncio.run() per request (the old behaviour) reusing it on the 2nd
        # request raised "bound to a different loop". The persistent loop fixes it.
        if state["lock"] is None:
            state["lock"] = asyncio.Lock()
        async with state["lock"]:
            state["counter"] += 1
        return {"counter": state["counter"]}

    def run_server():
        try:
            app.run(host="127.0.0.1", port=ASYNC_PORT)
        except Exception:
            pass

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.6)
    yield ASYNC_BASE


@pytest.mark.integration
def test_loop_bound_resource_survives_across_requests(async_server):
    """C2/C3: a loop-bound asyncio.Lock reused across requests must not error."""
    r1 = requests.get(f"{async_server}/loopbound")
    r2 = requests.get(f"{async_server}/loopbound")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["counter"] == 1
    assert r2.json()["counter"] == 2


@pytest.mark.integration
def test_async_startup_hook_runs(async_server):
    """C4: an async def startup hook actually executes."""
    resp = requests.get(f"{async_server}/started")
    assert resp.status_code == 200
    assert resp.json()["started"] is True


# =============================================================================
# Batch 4: Python API fixes (CsrfConfig honored by app.use)
# =============================================================================

CSRF_PORT = 18133
CSRF_BASE = f"http://127.0.0.1:{CSRF_PORT}"


@pytest.fixture(scope="module")
def csrf_server():
    """Server whose CSRF cookie name is customised via app.use(CsrfConfig(...))."""
    from cello import App
    from cello.middleware import CsrfConfig

    app = App()
    app.use(CsrfConfig(cookie_name="mytoken", header_name="X-My-Csrf"))

    @app.get("/")
    def home(req):
        return {"ok": True}

    def run_server():
        try:
            app.run(host="127.0.0.1", port=CSRF_PORT)
        except Exception:
            pass

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.5)
    yield CSRF_BASE


@pytest.mark.integration
def test_csrf_config_cookie_name_is_honored(csrf_server):
    """P1: CsrfConfig passed to app.use() is applied, not silently ignored."""
    resp = requests.get(f"{csrf_server}/")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "mytoken=" in set_cookie
