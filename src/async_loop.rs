//! A single persistent asyncio event loop running on a dedicated daemon thread.
//!
//! Async Python handlers (and lifecycle hooks) are submitted to this loop via
//! `asyncio.run_coroutine_threadsafe` instead of a fresh `asyncio.run()` per call.
//!
//! Why this matters:
//! - **Loop-bound resources survive.** A module-level `aiohttp.ClientSession`,
//!   `asyncpg`/SQLAlchemy async pool, `asyncio.Lock`/`Queue`, etc. are bound to the
//!   loop that created them. With `asyncio.run()` (a fresh loop per request) the 2nd
//!   request raised `RuntimeError: ... attached to a different loop`. One persistent
//!   loop fixes that.
//! - **Real concurrency.** The wait inside `concurrent.futures.Future.result()`
//!   releases the GIL, so while one coroutine awaits I/O the loop runs others and the
//!   GIL is not pinned for the coroutine's whole lifetime.

use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::sync::OnceLock;

/// The process-wide persistent event loop (a Python `asyncio` loop object).
static EVENT_LOOP: OnceLock<Py<PyAny>> = OnceLock::new();

/// Python helper that creates a new event loop and runs it forever on a daemon
/// thread, returning the loop object so coroutines can be scheduled onto it.
const RUNNER_SRC: &str = r#"
import asyncio
import threading


def start_loop():
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run, name="cello-asyncio", daemon=True).start()
    return loop
"#;

fn create_loop(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let module = PyModule::from_code(py, RUNNER_SRC, "cello_async_loop.py", "cello_async_loop")?;
    let loop_obj = module.getattr("start_loop")?.call0()?;
    Ok(loop_obj.into_py(py))
}

/// Return the persistent event loop, starting it on first use.
pub fn get_loop(py: Python<'_>) -> PyResult<Py<PyAny>> {
    if let Some(l) = EVENT_LOOP.get() {
        return Ok(l.clone_ref(py));
    }
    // Initialisation runs while holding the GIL, so concurrent callers are largely
    // serialised; in the worst case a rare race starts a second (idle) loop whose
    // handle is dropped. `OnceLock` guarantees a single winner is ever observed.
    let loop_obj = create_loop(py)?;
    match EVENT_LOOP.set(loop_obj.clone_ref(py)) {
        Ok(()) => Ok(loop_obj),
        Err(_) => Ok(EVENT_LOOP
            .get()
            .expect("event loop set by the winning thread")
            .clone_ref(py)),
    }
}

/// Eagerly start the loop. Call once at server startup (while single-threaded) so
/// the first request never has to pay initialisation cost or race on it.
pub fn ensure_started(py: Python<'_>) {
    if let Err(e) = get_loop(py) {
        eprintln!("Warning: failed to start persistent asyncio loop: {e}");
    }
}

/// Drive a coroutine on the persistent loop to completion and return its result.
///
/// MUST be called from a blocking context (e.g. `tokio::task::spawn_blocking`): the
/// wait inside `Future.result()` releases the GIL so the loop and other coroutines
/// keep running concurrently.
pub fn run_coroutine_blocking(py: Python<'_>, coro: &PyAny) -> Result<PyObject, String> {
    let event_loop = get_loop(py).map_err(|e| e.to_string())?;
    let asyncio = py.import("asyncio").map_err(|e| e.to_string())?;
    let cfut = asyncio
        .call_method1("run_coroutine_threadsafe", (coro, event_loop.as_ref(py)))
        .map_err(|e| e.to_string())?;
    // Blocks until the coroutine finishes; the internal wait releases the GIL.
    cfut.call_method0("result")
        .map(|r| r.into_py(py))
        .map_err(|e| e.to_string())
}
