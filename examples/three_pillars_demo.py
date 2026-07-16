"""Three-pillar upgrades demo — Speed, Simplicity, Security.

Run:
    python examples/three_pillars_demo.py     # http://127.0.0.1:8080

Try:
    # Security — full headers (CSP, Permissions-Policy, cross-origin isolation)
    curl -i http://127.0.0.1:8080/

    # Speed — cached + gzip-compressed on the 2nd hit
    curl -s -D - -o /dev/null --compressed http://127.0.0.1:8080/report   # MISS
    curl -s -D - -o /dev/null --compressed http://127.0.0.1:8080/report   # HIT + gzip

    # Simplicity/Security — request body validation (400 on bad input)
    curl -XPOST http://127.0.0.1:8080/users -d '{"name":"Ada","age":36}'  # 200
    curl -XPOST http://127.0.0.1:8080/users -d '{"name":"Ada","age":"x"}' # 400
"""

from cello import App, CSP, SecurityHeadersConfig

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    BaseModel = None

app = App()

# ── Security: full security headers in one call ──────────────────────────────
app.enable_security_headers(SecurityHeadersConfig(
    x_frame_options="DENY",
    hsts_max_age=31_536_000,
    hsts_include_subdomains=True,
    csp=CSP().default_src(["'self'"]).img_src(["'self'", "data:"]),
    permissions_policy={"geolocation": [], "camera": ["'self'"]},
    coep="require-corp",
    coop="same-origin",
    corp="same-origin",
))

# ── Speed: caching now gzips HITs inline for gzip-capable clients ────────────
app.enable_compression(min_size=256)
app.enable_caching(ttl=30)          # compress=True by default


@app.get("/")
def index(request):
    return {"pillars": ["speed", "simplicity", "security"]}


@app.get("/report")
def report(request):
    # Large, cacheable, compressible payload.
    return {"rows": [{"i": i, "v": "x" * 40} for i in range(200)]}


# ── Simplicity + Security: declarative request validation ────────────────────
if BaseModel is not None:

    class UserDTO(BaseModel):
        name: str
        age: int

    @app.post("/users", body=UserDTO)
    def create_user(request, user):     # `user` is validated; 400 on bad input
        return {"id": 1, "name": user.name, "age": user.age}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
