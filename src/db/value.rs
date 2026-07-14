//! Value conversion between Python objects and SQL / Redis wire types.
//!
//! This is the crux of the native data layer: Python is dynamically typed, but
//! Postgres parameters and results are strongly typed. We convert Python values
//! into a small [`SqlParam`] enum (which implements [`ToSql`] by dispatching on
//! the target column type) and convert result rows back into Python objects by
//! matching on each column's Postgres [`Type`].

use std::error::Error;

use bytes::BytesMut;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes};
use tokio_postgres::types::{to_sql_checked, IsNull, ToSql, Type};
use tokio_postgres::Row;

/// A dynamically-typed SQL parameter converted from a Python object.
///
/// Encoding is chosen at bind time from the *target* Postgres type, so a Python
/// `int` binds correctly whether the column is `int2`, `int4`, `int8`, or a
/// float. Types we do not encode natively (e.g. `numeric`, `timestamptz`) work
/// via an explicit cast in SQL — e.g. `$1::numeric` — because the cast makes the
/// inferred parameter type `text`, which we always encode.
#[derive(Debug, Clone)]
pub enum SqlParam {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Json(serde_json::Value),
}

/// Convert a single Python object into a [`SqlParam`].
///
/// Order matters: `bool` is a subclass of `int` in Python, so it must be checked
/// before the integer branch.
pub fn py_to_sqlparam(obj: &PyAny) -> PyResult<SqlParam> {
    if obj.is_none() {
        return Ok(SqlParam::Null);
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        return Ok(SqlParam::Bool(b.is_true()));
    }
    if let Ok(i) = obj.extract::<i64>() {
        return Ok(SqlParam::Int(i));
    }
    if let Ok(f) = obj.extract::<f64>() {
        return Ok(SqlParam::Float(f));
    }
    if let Ok(s) = obj.extract::<String>() {
        return Ok(SqlParam::Text(s));
    }
    if let Ok(b) = obj.downcast::<PyBytes>() {
        return Ok(SqlParam::Bytes(b.as_bytes().to_vec()));
    }
    // Fallback: dict / list / anything JSON-serialisable → json/jsonb.
    let json_mod = obj.py().import("json")?;
    let dumped: String = json_mod.call_method1("dumps", (obj,))?.extract()?;
    let value: serde_json::Value = serde_json::from_str(&dumped).map_err(|e| {
        pyo3::exceptions::PyTypeError::new_err(format!(
            "unsupported SQL parameter type ({}): {e}",
            obj.get_type().name().unwrap_or("?")
        ))
    })?;
    Ok(SqlParam::Json(value))
}

/// Convert a Python sequence of parameters into `SqlParam`s.
pub fn py_params_to_sqlparams(params: &[&PyAny]) -> PyResult<Vec<SqlParam>> {
    params.iter().map(|p| py_to_sqlparam(p)).collect()
}

impl ToSql for SqlParam {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match self {
            SqlParam::Null => Ok(IsNull::Yes),
            SqlParam::Bool(b) => b.to_sql(ty, out),
            SqlParam::Int(i) => match *ty {
                Type::INT2 => (*i as i16).to_sql(ty, out),
                Type::INT4 => (*i as i32).to_sql(ty, out),
                Type::FLOAT4 => (*i as f32).to_sql(ty, out),
                Type::FLOAT8 => (*i as f64).to_sql(ty, out),
                // int8 and everything else that accepts an integer.
                _ => i.to_sql(ty, out),
            },
            SqlParam::Float(f) => match *ty {
                Type::FLOAT4 => (*f as f32).to_sql(ty, out),
                _ => f.to_sql(ty, out),
            },
            SqlParam::Text(s) => s.to_sql(ty, out),
            SqlParam::Bytes(b) => b.to_sql(ty, out),
            SqlParam::Json(v) => v.to_sql(ty, out),
        }
    }

    // We dispatch on the target type inside `to_sql`, so accept everything and
    // let Postgres reject genuine mismatches at execution time.
    fn accepts(_ty: &Type) -> bool {
        true
    }

    to_sql_checked!();
}

/// Convert a full Postgres [`Row`] into a Python `dict` keyed by column name.
pub fn row_to_pydict(py: Python<'_>, row: &Row) -> PyResult<PyObject> {
    let dict = pyo3::types::PyDict::new(py);
    for (idx, col) in row.columns().iter().enumerate() {
        let value = pg_value_to_py(py, row, idx, col.type_())?;
        dict.set_item(col.name(), value)?;
    }
    Ok(dict.into_py(py))
}

/// Decode one column of a row into a Python object based on its Postgres type.
///
/// Supports the common scalar types. Unknown types fall back to a text decode,
/// and finally to a diagnostic placeholder so a single exotic column can never
/// crash a whole query.
fn pg_value_to_py(py: Python<'_>, row: &Row, idx: usize, ty: &Type) -> PyResult<PyObject> {
    macro_rules! get {
        ($t:ty) => {
            row.try_get::<_, Option<$t>>(idx)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?
        };
    }

    let obj = match *ty {
        Type::BOOL => get!(bool).into_py(py),
        Type::INT2 => get!(i16).into_py(py),
        Type::INT4 => get!(i32).into_py(py),
        Type::INT8 => get!(i64).into_py(py),
        Type::OID => get!(u32).into_py(py),
        Type::FLOAT4 => get!(f32).into_py(py),
        Type::FLOAT8 => get!(f64).into_py(py),
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN | Type::CHAR => {
            get!(String).into_py(py)
        }
        Type::BYTEA => match get!(Vec<u8>) {
            Some(b) => PyBytes::new(py, &b).into_py(py),
            None => py.None(),
        },
        Type::JSON | Type::JSONB => match get!(serde_json::Value) {
            Some(v) => json_to_py(py, &v)?,
            None => py.None(),
        },
        Type::UUID => match get!(uuid::Uuid) {
            Some(u) => u.to_string().into_py(py),
            None => py.None(),
        },
        Type::TIMESTAMP => match get!(chrono::NaiveDateTime) {
            Some(t) => t.format("%Y-%m-%dT%H:%M:%S%.6f").to_string().into_py(py),
            None => py.None(),
        },
        Type::TIMESTAMPTZ => match get!(chrono::DateTime<chrono::Utc>) {
            Some(t) => t.to_rfc3339().into_py(py),
            None => py.None(),
        },
        Type::DATE => match get!(chrono::NaiveDate) {
            Some(d) => d.to_string().into_py(py),
            None => py.None(),
        },
        Type::TIME => match get!(chrono::NaiveTime) {
            Some(t) => t.to_string().into_py(py),
            None => py.None(),
        },
        // Best-effort fallback: try text, then a diagnostic placeholder.
        _ => match row.try_get::<_, Option<String>>(idx) {
            Ok(Some(s)) => s.into_py(py),
            Ok(None) => py.None(),
            Err(_) => format!("<unsupported column type: {ty}>").into_py(py),
        },
    };
    Ok(obj)
}

/// Recursively convert a `serde_json::Value` into a native Python object.
pub fn json_to_py(py: Python<'_>, value: &serde_json::Value) -> PyResult<PyObject> {
    use serde_json::Value;
    let obj = match value {
        Value::Null => py.None(),
        Value::Bool(b) => b.into_py(py),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_py(py)
            } else if let Some(u) = n.as_u64() {
                u.into_py(py)
            } else {
                n.as_f64().unwrap_or(0.0).into_py(py)
            }
        }
        Value::String(s) => s.into_py(py),
        Value::Array(arr) => {
            let list = pyo3::types::PyList::empty(py);
            for item in arr {
                list.append(json_to_py(py, item)?)?;
            }
            list.into_py(py)
        }
        Value::Object(map) => {
            let dict = pyo3::types::PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, json_to_py(py, v)?)?;
            }
            dict.into_py(py)
        }
    };
    Ok(obj)
}
