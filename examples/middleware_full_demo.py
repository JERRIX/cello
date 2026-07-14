#!/usr/bin/env python3
"""
Full middleware demo — the 7 core `enable_*` middleware, each with a route that
makes its effect observable. All are real and verified end-to-end.

  enable_cors            -> Access-Control-Allow-Origin on responses
  enable_logging         -> request/response lines on stdout
  enable_compression     -> gzip for responses >= min_size with Accept-Encoding
  enable_caching         -> X-Cache: MISS then HIT; handler skipped on HIT
  enable_prometheus      -> GET /metrics in Prometheus text format
  enable_rate_limit      -> 429 + X-RateLimit-* once the token bucket empties
  enable_circuit_breaker -> 503 fast-fail after repeated 5xx on a route

Run:
    python examples/middleware_full_demo.py

Try:
    curl -i -H "Origin: http://example.com" http://127.0.0.1:8000/          # CORS header
    curl -i --compressed http://127.0.0.1:8000/big                          # Content-Encoding: gzip
    curl -i http://127.0.0.1:8000/time; curl -i http://127.0.0.1:8000/time  # X-Cache MISS then HIT
    curl http://127.0.0.1:8000/metrics                                      # Prometheus text
    for i in $(seq 25); do curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/limited; done; echo
    sleep 5  # let the shared token bucket refill before the next demo
    for i in $(seq 5); do curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/flaky; done; echo

Author: Jagadeesh Katla
"""

import time

from cello import App, Response, RateLimitConfig

app = App()

app.enable_cors(origins=["http://example.com", "http://localhost:3000"])
app.enable_logging()
app.enable_compression(min_size=512)                      # gzip responses >= 512 bytes
app.enable_caching(ttl=10)                                # cache GETs for 10s
app.enable_prometheus(endpoint="/metrics", namespace="demo")
# Rate limiting and the circuit breaker are BOTH global. The token bucket is
# shared across all routes, so a burst on /limited also consumes budget for
# /flaky — run the two curl loops a second or two apart. Capacity is generous
# here so ordinary browsing of the other routes is never throttled.
app.enable_rate_limit(RateLimitConfig(algorithm="token_bucket", capacity=20, refill_rate=5))
app.enable_circuit_breaker(failure_threshold=3, reset_timeout=15)


@app.get("/")
def home(request):
    return {"message": "middleware demo", "routes": ["/big", "/time", "/metrics", "/limited", "/flaky"]}


@app.get("/big")
def big(request):
    return {"payload": "cello " * 500}          # large enough to compress


@app.get("/time")
def now(request):
    return {"server_time": time.time()}         # frozen while cached (proves HIT)


@app.get("/limited")
def limited(request):
    return {"ok": True}


@app.get("/flaky")
def flaky(request):
    return Response.json({"error": "upstream unavailable"}, status=500)   # trips the breaker


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
