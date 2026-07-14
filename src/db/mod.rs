//! Native async data layer: real Postgres pool + Redis client.
//!
//! Replaces the previous mock scaffolding (`middleware::database` /
//! `middleware::redis`) with connection pools that actually talk to a server.
//! Both expose Python awaitables through `pyo3_asyncio::tokio::future_into_py`,
//! matching the proven `AsyncClient` bridge (see `src/http_client.rs`).

pub mod postgres;
pub mod redis_client;
pub mod value;

pub use postgres::{PyDatabase, PyTransaction};
pub use redis_client::PyRedis;
