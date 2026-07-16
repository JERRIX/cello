"""Regression tests for the three-pillar feature upgrades (v1.3.0):

* Speed      — cache HIT serves a gzip-compressed body (was uncompressed).
* Security   — full security headers (CSP, Permissions-Policy, COEP/COOP/CORP)
               configurable from Python; Redis `rediss://` (TLS) URLs accepted.
* Simplicity — the announce-only enable_* stubs are honest (no crash).
* DX+Security — `@app.post(..., body=DTO)` validates the JSON body (400 on error).
"""

import threading
import time

import pytest

requests = pytest.importorskip("requests")

from cello import App, Response, CSP, SecurityHeadersConfig, RedisConfig

_PORT = [20800]


def _serve(app):
    _PORT[0] += 1
    p = _PORT[0]
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=p), daemon=True).start()
    base = f"http://127.0.0.1:{p}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            requests.get(base + "/__ping__", timeout=0.5)
            return base
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"server on :{p} never came up")


def _ping_app():
    app = App()

    @app.get("/__ping__")
    def ping(request):
        return {"ok": True}

    return app


# ── Speed: compressed cache ─────────────────────────────────────────────────

def test_cache_hit_is_gzip_compressed():
    app = _ping_app()
    app.enable_caching(ttl=30)  # compress defaults to True

    @app.get("/big")
    def big(request):
        return {"data": "x" * 5000}

    url = _serve(app)
    requests.get(f"{url}/big")  # MISS (populate cache)
    hit = requests.get(f"{url}/big")  # HIT (requests accepts gzip by default)
    assert hit.headers.get("x-cache") == "HIT"
    assert hit.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in hit.headers.get("vary", "").lower()
    assert hit.json()["data"] == "x" * 5000  # transparently decoded


def test_cache_hit_identity_for_non_gzip_client():
    app = _ping_app()
    app.enable_caching(ttl=30)

    @app.get("/big")
    def big(request):
        return {"data": "y" * 5000}

    url = _serve(app)
    requests.get(f"{url}/big", headers={"Accept-Encoding": "identity"})
    hit = requests.get(f"{url}/big", headers={"Accept-Encoding": "identity"})
    assert "gzip" not in (hit.headers.get("content-encoding") or "")


def test_cache_compress_can_be_disabled():
    app = _ping_app()
    app.enable_caching(ttl=30, compress=False)

    @app.get("/big")
    def big(request):
        return {"data": "z" * 5000}

    url = _serve(app)
    requests.get(f"{url}/big")
    hit = requests.get(f"{url}/big")
    assert hit.headers.get("x-cache") == "HIT"
    assert "gzip" not in (hit.headers.get("content-encoding") or "")


# ── Security: full headers ──────────────────────────────────────────────────

def test_full_security_headers():
    app = _ping_app()
    csp = CSP().default_src(["'self'"]).img_src(["'self'", "data:"])
    app.enable_security_headers(SecurityHeadersConfig(
        csp=csp,
        permissions_policy={"geolocation": [], "camera": ["'self'"]},
        coep="require-corp",
        coop="same-origin",
        corp="same-origin",
    ))
    url = _serve(app)
    r = requests.get(f"{url}/__ping__")
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
    assert "geolocation=()" in r.headers.get("permissions-policy", "")
    assert r.headers.get("cross-origin-embedder-policy") == "require-corp"
    assert r.headers.get("cross-origin-opener-policy") == "same-origin"
    assert r.headers.get("cross-origin-resource-policy") == "same-origin"


def test_secure_preset_includes_cross_origin_isolation():
    app = _ping_app()
    app.enable_security_headers(SecurityHeadersConfig.secure())
    url = _serve(app)
    r = requests.get(f"{url}/__ping__")
    assert r.headers.get("cross-origin-opener-policy") == "same-origin"
    assert r.headers.get("cross-origin-embedder-policy") == "require-corp"


def test_redis_tls_url_accepted():
    """`rediss://` URLs must be accepted (TLS feature compiled in). Connection is
    lazy, so no live server is required."""
    app = App()
    app.enable_redis(RedisConfig(url="rediss://localhost:6379"))  # must not raise
    app.enable_redis(RedisConfig(url="redis://localhost:6379"))   # plaintext still ok


# ── DX + Security: request validation ───────────────────────────────────────

def test_body_validation():
    pydantic = pytest.importorskip("pydantic")

    class UserDTO(pydantic.BaseModel):
        name: str
        age: int

    app = _ping_app()

    @app.post("/users", body=UserDTO)
    def create(request, user):
        return {"created": True, "name": user.name, "age": user.age}

    url = _serve(app)

    ok = requests.post(f"{url}/users", json={"name": "Ada", "age": 36})
    assert ok.status_code == 200
    assert ok.json() == {"created": True, "name": "Ada", "age": 36}

    bad = requests.post(f"{url}/users", json={"name": "Ada", "age": "NaN"})
    assert bad.status_code == 400
    assert "detail" in bad.json()

    malformed = requests.post(
        f"{url}/users", data="{bad json", headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 400


def test_body_validation_plain_class():
    """`body=` works with a plain (non-pydantic) class constructed from the JSON."""
    class Point:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    app = _ping_app()

    @app.post("/points", body=Point)
    def create(request, p):
        return {"x": p.x, "y": p.y}

    url = _serve(app)
    ok = requests.post(f"{url}/points", json={"x": 1, "y": 2})
    assert ok.status_code == 200 and ok.json() == {"x": 1, "y": 2}

    # Missing field -> constructor TypeError -> 400
    bad = requests.post(f"{url}/points", json={"x": 1})
    assert bad.status_code == 400


# ── Simplicity: announce-only stubs don't break the server ──────────────────

@pytest.mark.parametrize("enable", [
    lambda app: app.enable_cqrs(),
    lambda app: app.enable_saga(),
    lambda app: app.enable_event_sourcing(),
])
def test_stub_plugins_are_harmless(enable):
    app = _ping_app()
    enable(app)
    url = _serve(app)
    assert requests.get(f"{url}/__ping__").status_code == 200
