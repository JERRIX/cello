# Core middleware (`enable_*`)

Cello's middleware runs in Rust on the hot path. Enable a piece with one call on
the `App`; the effect is applied to every route. All 7 below are verified
end-to-end (see `tests/test_middleware.py` and `examples/middleware_full_demo.py`).

```python
from cello import App, RateLimitConfig

app = App()
app.enable_cors(origins=["http://example.com"])
app.enable_logging()
app.enable_compression(min_size=1024)
app.enable_caching(ttl=60)
app.enable_prometheus(endpoint="/metrics")
app.enable_rate_limit(RateLimitConfig(algorithm="token_bucket", capacity=100, refill_rate=10))
app.enable_circuit_breaker(failure_threshold=5, reset_timeout=30)
```

## `enable_cors(origins=None)`

Adds CORS headers. When `origins` is given, a request whose `Origin` matches gets
`Access-Control-Allow-Origin: <origin>` (with `Vary: Origin`).

```python
app.enable_cors(origins=["http://example.com", "http://localhost:3000"])
```

```console
$ curl -i -H "Origin: http://example.com" http://127.0.0.1:8000/
access-control-allow-origin: http://example.com
```

!!! note "Preflight limitation"
    A preflight `OPTIONS` request to a path that has **no registered route** is
    answered with 404 before the CORS layer runs (the router fast-returns 404 on
    a miss). Register an `OPTIONS` route for paths that need non-simple CORS, e.g.
    `@app.options("/api/thing")`. Simple requests (GET/POST) are unaffected.

## `enable_logging()`

Logs each request and its response to stdout.

```
--> GET /
<-- GET / 200 OK
```

## `enable_compression(min_size=None)`

gzip-compresses responses whose body is at least `min_size` bytes (default 1024)
when the client sends `Accept-Encoding: gzip`. Adds `Content-Encoding: gzip`.

```console
$ curl -i --compressed http://127.0.0.1:8000/big
content-encoding: gzip
```

!!! note "Interaction with caching"
    A cache **HIT** serves the stored body and bypasses compression, so cached
    responses are returned uncompressed. If both matter for a large endpoint,
    exclude it from the cache (`exclude_paths=[...]`).

## `enable_caching(ttl=300, methods=None, exclude_paths=None)`

Caches responses in memory for `ttl` seconds. Adds `X-Cache: MISS` on the first
request and `X-Cache: HIT` on subsequent ones; on a HIT the handler is skipped.

```python
app.enable_caching(ttl=60, methods=["GET"], exclude_paths=["/metrics", "/big"])
```

```console
$ curl -si http://127.0.0.1:8000/time | grep -i x-cache   # X-Cache: MISS
$ curl -si http://127.0.0.1:8000/time | grep -i x-cache   # X-Cache: HIT
```

Invalidate tagged entries with `app.invalidate_cache(tags)` (set the `X-Cache-Tags`
response header to tag an entry).

## `enable_prometheus(endpoint="/metrics", namespace="cello", subsystem="http")`

Records request counts, in-progress gauge, and latency histograms, and serves
them in Prometheus text format at `endpoint`.

```console
$ curl http://127.0.0.1:8000/metrics
# HELP cello_http_request_duration_seconds HTTP request latencies in seconds
# TYPE cello_http_request_duration_seconds histogram
cello_http_request_duration_seconds_bucket{method="GET",path="/",status="200",le="0.005"} 1
...
```

Metric names are prefixed with `<namespace>_<subsystem>_`.

## `enable_rate_limit(config)`

Throttles requests. `RateLimitConfig(algorithm=..., capacity=..., refill_rate=...)`
supports `"token_bucket"`, `"sliding_window"`, and `"adaptive"`. Over the limit
returns **429** with `X-RateLimit-Limit/Remaining/Reset` and `Retry-After`.

```python
app.enable_rate_limit(RateLimitConfig(algorithm="token_bucket", capacity=5, refill_rate=1))
```

```console
$ for i in $(seq 8); do curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/limited; done
200 200 200 200 200 429 429 429
```

## `enable_circuit_breaker(failure_threshold=5, reset_timeout=30, half_open_target=3, failure_codes=None)`

Trips per route after `failure_threshold` responses with a failure status
(default `500, 502, 503, 504`), then fast-fails with **503** for `reset_timeout`
seconds before probing again (half-open). Healthy routes are unaffected.

```console
$ for i in $(seq 5); do curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/flaky; done
500 500 503 503 503
```

## Runnable example

[`examples/middleware_full_demo.py`](https://github.com/jagadeesh32/cello/blob/main/examples/middleware_full_demo.py)
wires up all seven with a route that makes each one observable.
