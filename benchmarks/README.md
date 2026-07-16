# Cello Benchmarks

Benchmark suite for measuring Cello framework performance.

## Quick Start

### Option 1: Quick Benchmark (No Dependencies)

```bash
# Terminal 1 - Start the server (single worker)
python benchmarks/quick_bench.py --server

# Terminal 2 - Run benchmark
python benchmarks/quick_bench.py --bench
```

### Option 2: Multi-Worker Benchmark (Recommended)

For maximum throughput, use multiple worker processes:

```bash
# Terminal 1 - Start with N workers (e.g., 4 for 4-core machine)
python benchmarks/quick_bench.py --server --workers 4

# Terminal 2 - Run wrk benchmark
wrk -t12 -c400 -d10s http://127.0.0.1:8080/
wrk -t12 -c400 -d10s http://127.0.0.1:8080/json
```

### Option 3: Full Benchmark Suite

Requires: `pip install aiohttp`

```bash
# Terminal 1 - Start the server
python benchmarks/benchmark.py --server

# Terminal 2 - Run benchmark
python benchmarks/benchmark.py --client --concurrency 100 --duration 10
```

### Option 4: Using wrk (Recommended for accurate results)

Install wrk: `sudo apt install wrk` (Linux) or `brew install wrk` (macOS).

```bash
# Terminal 1 - Start the server with workers matching core count
python benchmarks/quick_bench.py --server --workers $(nproc)

# Terminal 2 - Run wrk
wrk -t12 -c400 -d10s http://127.0.0.1:8080/
wrk -t12 -c400 -d10s http://127.0.0.1:8080/json
```

## Benchmark Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Simple JSON response |
| `GET /json` | JSON with nested data |
| `POST /echo` | Echo POST body |

## Metrics Measured

- **RPS** - Requests per second
- **Latency** - Average, min, max, p50, p95, p99
- **Throughput** - MB/s transferred
- **Errors** - Failed requests

## Expected Results

### Single Worker (1 process)

On a modern machine, expect per-worker:

- Simple JSON: ~25,000-35,000 RPS
- JSON with data: ~20,000-25,000 RPS
- Path parameters: ~25,000-35,000 RPS

### Multi-Worker Mode (recommended for benchmarks)

With `--workers N`, Cello forks N+1 processes (N children + parent), all serving via SO_REUSEPORT.
Each worker runs a single-threaded Tokio event loop for zero GIL contention.
Expect near-linear scaling:

| Workers | Processes | Expected RPS (JSON) |
|---------|-----------|-------------------|
| 2       | 3         | ~70,000-90,000    |
| 4       | 5         | ~160,000-175,000  |
| 8       | 9         | ~200,000+         |

**Reference benchmark**: ~135,000 req/s with 4 workers (5 processes) using `wrk -t12 -c400 -d10s` on an 8-core WSL2 box (client + server sharing cores); dedicated hardware scales higher.

### Platform Notes

- **Native Linux (x86_64)**: Best performance. Intel Xeon / AMD EPYC recommended for production benchmarks.
- **WSL2**: Expect ~40-60% of native performance due to virtual network adapter overhead.
- **macOS**: Good performance on Apple Silicon. Use `wrk` for accurate results.
- **Best practice**: Run wrk on a separate machine to avoid client/server CPU contention.

### How to Reproduce

Use the same machine, worker count, and wrk settings for repeatable results:

```bash
# Cello (4 workers)
python benchmarks/quick_bench.py --server --workers 4
wrk -t12 -c400 -d10s http://127.0.0.1:8080/
```

## Local wrk Run (2026-07-16)

Measured with **`wrk -t12 -c400 -d10s`** (warmed up, 3 runs per endpoint)
against a **release build** (`maturin develop --release`) on a **WSL2 dev
container, 8 cores shared between `wrk` and the server**. Because `wrk`'s 12
threads compete with the server processes for the same 8 cores, these land below
the dedicated-hardware reference above — but they confirm real, sustained
six-figure throughput with no regression.

| Workers (procs) | Endpoint | Req/sec (3-run range) | Avg latency |
|-----------------|----------|----------------------:|------------:|
| 4 (5) | `GET /` | **~138,000** (128k–138k) | 3.3–4.7 ms |
| 4 (5) | `GET /json` | **~134,000** (113k–141k) | 3.3–3.8 ms |
| 8 (9) | `GET /` | **~122,000** | 5.4 ms |
| 8 (9) | `GET /json` | ~80,000* | 5.8 ms |

\* At 8 workers the 9 server processes + `wrk`'s 12 threads oversubscribe the
8-core box, so `/json` becomes CPU-starved and noisy; the **4-worker** numbers
are the clean, representative measurement on this hardware.

> **Important:** a pure-Python load generator (e.g. `quick_bench.py`) is
> GIL-bound and opens a fresh connection per request, so it measures the
> *client*, not the server — it will report a small fraction of these numbers.
> Always benchmark with `wrk` (ideally from a **separate machine**, per the
> platform notes above) to see the server's real throughput.
