"""
End-to-end tests for the 7 core `enable_*` middleware.

Each test drives a real Cello server (daemon thread) and asserts the observable
effect: response headers, status codes, cache behaviour, and the Prometheus
endpoint. Rate limiting and the circuit breaker are global, so they get their own
isolated servers/ports.
"""

import threading
import time

import pytest

requests = pytest.importorskip("requests")

from cello import App, Response, RateLimitConfig


def _serve(app, port):
    """Start `app` on a daemon thread and wait until it accepts requests."""
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port), daemon=True).start()
    deadline = time.time() + 10
    last = None
    while time.time() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{port}/__ping__", timeout=0.5)
            return f"http://127.0.0.1:{port}"
        except Exception as e:  # not up yet
            last = e
            time.sleep(0.1)
    raise RuntimeError(f"server on :{port} never came up: {last}")


# ── Core middleware (cors, logging, compression, caching, prometheus) ─────────

@pytest.fixture(scope="module")
def core_server():
    app = App()
    app.enable_cors(origins=["http://example.com"])
    app.enable_logging()
    app.enable_compression(min_size=100)
    # NOTE: a cache HIT serves the stored (uncompressed) body and bypasses
    # compression, so exclude the compression route from caching here.
    app.enable_caching(ttl=30, exclude_paths=["/big"])
    app.enable_prometheus(endpoint="/metrics", namespace="test")

    @app.get("/__ping__")
    def ping(request):
        return {"ok": True}

    @app.get("/big")
    def big(request):
        return {"data": "x" * 3000}

    counter = {"n": 0}

    @app.get("/counter")
    def count(request):
        counter["n"] += 1
        return {"count": counter["n"]}

    return _serve(app, 18220)


def test_cors_reflects_allowed_origin(core_server):
    r = requests.get(f"{core_server}/big", headers={"Origin": "http://example.com"})
    assert r.headers.get("access-control-allow-origin") == "http://example.com"


def test_compression_gzip_when_requested(core_server):
    # requests sends Accept-Encoding: gzip by default and transparently decodes.
    r = requests.get(f"{core_server}/big")
    assert r.headers.get("content-encoding") == "gzip"
    # A raw request without gzip should not be compressed.
    raw = requests.get(f"{core_server}/big", headers={"Accept-Encoding": "identity"})
    assert "gzip" not in (raw.headers.get("content-encoding") or "")


def test_caching_hit_after_miss(core_server):
    r1 = requests.get(f"{core_server}/counter")
    r2 = requests.get(f"{core_server}/counter")
    assert r1.headers.get("x-cache") == "MISS"
    assert r2.headers.get("x-cache") == "HIT"
    # Handler is skipped on HIT, so the counter does not advance.
    assert r1.json()["count"] == r2.json()["count"]


def test_prometheus_metrics_endpoint(core_server):
    requests.get(f"{core_server}/big")  # generate some metrics
    r = requests.get(f"{core_server}/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    assert "test_http_request_duration_seconds" in r.text


# ── Rate limiting (isolated, global middleware) ───────────────────────────────

@pytest.fixture(scope="module")
def ratelimited_server():
    app = App()
    app.enable_rate_limit(RateLimitConfig(algorithm="token_bucket", capacity=3, refill_rate=1))

    @app.get("/__ping__")
    def ping(request):
        # The bucket has capacity 3; readiness poll uses this too, so give it a
        # moment to refill before the test asserts.
        return {"ok": True}

    url = _serve(app, 18221)
    time.sleep(1.5)  # let the token bucket refill after the readiness probe
    return url


def test_rate_limit_returns_429_after_capacity(ratelimited_server):
    codes = [requests.get(f"{ratelimited_server}/__ping__").status_code for _ in range(6)]
    assert 200 in codes
    assert 429 in codes
    # Once limited, the response carries rate-limit headers.
    limited = requests.get(f"{ratelimited_server}/__ping__")
    if limited.status_code == 429:
        assert "retry-after" in {k.lower() for k in limited.headers}


# ── Circuit breaker (isolated, global middleware) ─────────────────────────────

@pytest.fixture(scope="module")
def circuit_server():
    app = App()
    app.enable_circuit_breaker(failure_threshold=2, reset_timeout=30)

    @app.get("/__ping__")
    def ping(request):
        return {"ok": True}

    @app.get("/fail")
    def fail(request):
        return Response.json({"error": "boom"}, status=500)

    return _serve(app, 18222)


def test_circuit_breaker_opens_after_failures(circuit_server):
    codes = [requests.get(f"{circuit_server}/fail").status_code for _ in range(4)]
    # First failures pass through as 500, then the breaker opens and fast-fails 503.
    assert codes[0] == 500
    assert 503 in codes
    # The breaker is per-route: a healthy route is unaffected.
    assert requests.get(f"{circuit_server}/__ping__").status_code == 200
