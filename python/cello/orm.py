"""
Cello ORM — a small, Django-inspired async ORM over the native Postgres pool.

This is a *real, working* ORM built on ``app.database`` (deadpool-postgres). It
deliberately covers the common 80%: typed models, a chainable async QuerySet,
``create_table``, and ``ForeignKey``. It is **not** full Django parity — there
are no migrations diffing, no lazy reverse relations, no ``select_related`` join
planner, and no signals/admin. What is here is tested end-to-end against real
Postgres; where the line falls is documented in the class docstrings.

Quick start::

    from cello import App, DatabaseConfig
    from cello.orm import Model, AutoField, CharField, BooleanField, ForeignKey, setup

    class User(Model):
        id = AutoField()
        name = CharField(max_length=100)
        email = CharField(max_length=255, null=True)
        active = BooleanField(default=True)

    app = App()
    app.enable_database(DatabaseConfig(url="postgresql://postgres:postgres@localhost/db"))

    @app.on_event("startup")
    async def startup():
        setup(app.database)          # bind the ORM to the pool
        await User.create_table()

    @app.get("/users")
    async def users(request):
        rows = await User.objects.filter(active=True).order_by("-id").limit(20)
        return {"users": [u.to_dict() for u in rows]}

    @app.post("/users")
    async def create(request):
        data = request.json()
        u = await User.objects.create(name=data["name"], email=data.get("email"))
        return u.to_dict()
"""

from typing import Any, Optional

__all__ = [
    "setup",
    "Model",
    "Field",
    "AutoField",
    "IntegerField",
    "BigIntegerField",
    "FloatField",
    "BooleanField",
    "CharField",
    "TextField",
    "JSONField",
    "DateTimeField",
    "ForeignKey",
    "DoesNotExist",
    "MultipleObjectsReturned",
]


# ── Module-level default database binding ─────────────────────────────────────

_DEFAULT_DB = None


def setup(database) -> None:
    """Bind the ORM to a native ``Database`` pool (usually ``app.database``).

    Call once in an ``on_event("startup")`` hook. After this, model queries that
    do not pass ``.using(db)`` run against this pool.
    """
    global _DEFAULT_DB
    _DEFAULT_DB = database


def _require_db(db):
    db = db or _DEFAULT_DB
    if db is None:
        raise RuntimeError(
            "ORM is not bound to a database. Call cello.orm.setup(app.database) "
            "in a startup hook, or pass .using(request.database)."
        )
    return db


class DoesNotExist(Exception):
    """Raised by ``get()`` when no row matches."""


class MultipleObjectsReturned(Exception):
    """Raised by ``get()`` when more than one row matches."""


# ── Fields ────────────────────────────────────────────────────────────────────

class Field:
    """Base class for model fields."""

    # Overridden by subclasses; may be a callable(field) -> str for parametrised types.
    sql_type: Any = "TEXT"

    def __init__(
        self,
        primary_key: bool = False,
        null: bool = False,
        default: Any = None,
        unique: bool = False,
        db_column: Optional[str] = None,
    ):
        self.primary_key = primary_key
        self.null = null
        self.default = default
        self.unique = unique
        self.db_column = db_column
        self.name = None  # set by the metaclass

    @property
    def column(self) -> str:
        return self.db_column or self.name

    def ddl_type(self) -> str:
        return self.sql_type(self) if callable(self.sql_type) else self.sql_type

    def column_ddl(self) -> str:
        parts = [f'"{self.column}"', self.ddl_type()]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        if not self.null and not self.primary_key:
            parts.append("NOT NULL")
        if self.default is not None and not callable(self.default):
            parts.append(f"DEFAULT {_sql_literal(self.default)}")
        return " ".join(parts)


class AutoField(Field):
    """Auto-incrementing integer primary key (``SERIAL PRIMARY KEY``)."""

    sql_type = "SERIAL"

    def __init__(self, **kwargs):
        kwargs["primary_key"] = True
        super().__init__(**kwargs)


class IntegerField(Field):
    sql_type = "INTEGER"


class BigIntegerField(Field):
    sql_type = "BIGINT"


class FloatField(Field):
    sql_type = "DOUBLE PRECISION"


class BooleanField(Field):
    sql_type = "BOOLEAN"


class CharField(Field):
    def __init__(self, max_length: int = 255, **kwargs):
        self.max_length = max_length
        super().__init__(**kwargs)

    sql_type = staticmethod(lambda f: f"VARCHAR({f.max_length})")


class TextField(Field):
    sql_type = "TEXT"


class JSONField(Field):
    sql_type = "JSONB"


class DateTimeField(Field):
    sql_type = "TIMESTAMPTZ"

    def __init__(self, auto_now_add: bool = False, **kwargs):
        self.auto_now_add = auto_now_add
        if auto_now_add and "default" not in kwargs:
            kwargs["default"] = _NOW
        super().__init__(**kwargs)


class ForeignKey(Field):
    """A foreign-key column. Stored as ``<name>_id`` referencing the target's PK.

    ``to`` may be a Model class. This creates the integer column and a
    ``REFERENCES`` constraint; it does **not** add lazy relation attributes —
    load the related row explicitly with the target's queryset.
    """

    sql_type = "INTEGER"

    def __init__(self, to, on_delete: str = "CASCADE", **kwargs):
        self.to = to
        self.on_delete = on_delete
        super().__init__(**kwargs)

    @property
    def column(self) -> str:
        return self.db_column or f"{self.name}_id"

    def column_ddl(self) -> str:
        base = super().column_ddl()
        target = self.to._meta.table_name
        target_pk = self.to._meta.pk_column
        return f'{base} REFERENCES "{target}"("{target_pk}") ON DELETE {self.on_delete}'


_NOW = object()  # sentinel for DEFAULT now()


def _sql_literal(value: Any) -> str:
    if value is _NOW:
        return "now()"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    # Strings: single-quote and escape. Only used for DDL defaults (not user input).
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


# ── Model metaclass ───────────────────────────────────────────────────────────

class _Meta:
    def __init__(self, table_name, fields, pk_name, pk_column):
        self.table_name = table_name
        self.fields = fields              # dict name -> Field
        self.pk_name = pk_name
        self.pk_column = pk_column


class ModelMeta(type):
    def __new__(mcls, name, bases, namespace):
        if name == "Model":
            return super().__new__(mcls, name, bases, namespace)

        fields = {}
        for key, value in list(namespace.items()):
            if isinstance(value, Field):
                value.name = key
                fields[key] = value
                del namespace[key]

        # Inherit fields from base models.
        for base in bases:
            if hasattr(base, "_meta"):
                for k, v in base._meta.fields.items():
                    fields.setdefault(k, v)

        pk_name = next((n for n, f in fields.items() if f.primary_key), None)
        meta_opts = namespace.get("Meta")
        table_name = getattr(meta_opts, "table_name", None) or name.lower()
        pk_column = fields[pk_name].column if pk_name else None

        cls = super().__new__(mcls, name, bases, namespace)
        cls._meta = _Meta(table_name, fields, pk_name, pk_column)
        cls.objects = Manager(cls)
        cls.DoesNotExist = type("DoesNotExist", (DoesNotExist,), {})
        cls.MultipleObjectsReturned = type(
            "MultipleObjectsReturned", (MultipleObjectsReturned,), {}
        )
        return cls


class Model(metaclass=ModelMeta):
    """Base class for ORM models. Define fields as class attributes."""

    def __init__(self, **kwargs):
        for name in self._meta.fields:
            setattr(self, name, kwargs.get(name))
        # Allow FK ``<name>_id`` values too.
        for name, field in self._meta.fields.items():
            if isinstance(field, ForeignKey) and field.column in kwargs:
                setattr(self, field.column, kwargs[field.column])

    def to_dict(self) -> dict:
        out = {}
        for name, field in self._meta.fields.items():
            col = field.column
            if isinstance(field, ForeignKey):
                out[col] = getattr(self, col, getattr(self, name, None))
            else:
                out[name] = getattr(self, name, None)
        return out

    @classmethod
    def _from_row(cls, row: dict) -> "Model":
        obj = cls.__new__(cls)
        for name, field in cls._meta.fields.items():
            setattr(obj, name, row.get(field.column))
            if isinstance(field, ForeignKey):
                setattr(obj, field.column, row.get(field.column))
        return obj

    async def save(self, using=None) -> "Model":
        """Insert (no PK value) or update (PK present) this row."""
        db = _require_db(using)
        meta = self._meta
        pk_val = getattr(self, meta.pk_name, None) if meta.pk_name else None

        writable = [
            (name, f)
            for name, f in meta.fields.items()
            if not (isinstance(f, AutoField) and pk_val is None)
        ]
        if pk_val is None:
            cols, placeholders, params = [], [], []
            for i, (name, f) in enumerate(
                [(n, f) for n, f in meta.fields.items() if not isinstance(f, AutoField)],
                start=1,
            ):
                cols.append(f'"{f.column}"')
                placeholders.append(f"${i}")
                params.append(_field_value(self, name, f))
            sql = (
                f'INSERT INTO "{meta.table_name}" ({", ".join(cols)}) '
                f'VALUES ({", ".join(placeholders)}) RETURNING *'
            )
            row = await db.fetchrow(sql, *params)
            for name, f in meta.fields.items():
                setattr(self, name, row.get(f.column))
            return self
        else:
            sets, params = [], []
            idx = 1
            for name, f in meta.fields.items():
                if name == meta.pk_name:
                    continue
                sets.append(f'"{f.column}" = ${idx}')
                params.append(_field_value(self, name, f))
                idx += 1
            params.append(pk_val)
            sql = (
                f'UPDATE "{meta.table_name}" SET {", ".join(sets)} '
                f'WHERE "{meta.pk_column}" = ${idx}'
            )
            await db.execute(sql, *params)
            return self

    async def delete(self, using=None) -> int:
        db = _require_db(using)
        meta = self._meta
        pk_val = getattr(self, meta.pk_name)
        return await db.execute(
            f'DELETE FROM "{meta.table_name}" WHERE "{meta.pk_column}" = $1', pk_val
        )

    @classmethod
    async def create_table(cls, using=None, if_not_exists: bool = True) -> None:
        db = _require_db(using)
        cols = [f.column_ddl() for f in cls._meta.fields.values()]
        exists = "IF NOT EXISTS " if if_not_exists else ""
        sql = f'CREATE TABLE {exists}"{cls._meta.table_name}" ({", ".join(cols)})'
        await db.execute(sql)

    @classmethod
    async def drop_table(cls, using=None, if_exists: bool = True) -> None:
        db = _require_db(using)
        exists = "IF EXISTS " if if_exists else ""
        await db.execute(f'DROP TABLE {exists}"{cls._meta.table_name}"')

    def __repr__(self):
        pk = getattr(self, self._meta.pk_name, None) if self._meta.pk_name else None
        return f"<{self.__class__.__name__} pk={pk}>"


def _field_value(obj, name, field):
    if isinstance(field, ForeignKey):
        # Prefer the stored ``<name>_id`` column; allow assigning a Model instance.
        val = getattr(obj, field.column, None)
        if val is None:
            related = getattr(obj, name, None)
            if related is not None and hasattr(related, "_meta"):
                val = getattr(related, related._meta.pk_name, None)
        return val
    val = getattr(obj, name, None)
    if val is None and field.default is not None and not callable(field.default):
        return None if field.default is _NOW else field.default
    return val


# ── QuerySet / Manager ────────────────────────────────────────────────────────

_OPERATORS = {
    "exact": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "LIKE",
    "icontains": "ILIKE",
    "startswith": "LIKE",
    "istartswith": "ILIKE",
    "endswith": "LIKE",
    "in": "IN",
    "isnull": "IS",
}


class QuerySet:
    """A lazy, chainable, awaitable query.

    Chain ``filter``/``exclude``/``order_by``/``limit``/``offset`` (each returns a
    new QuerySet), then await it (or call ``all()``) to run it. Terminal helpers:
    ``get``, ``first``, ``count``, ``exists``, ``values``, ``delete``, ``update``.
    """

    def __init__(self, model, using=None):
        self.model = model
        self._db = using
        self._where = []       # list of (sql_fragment, params_list)
        self._order = []
        self._limit = None
        self._offset = None

    def _clone(self):
        qs = QuerySet(self.model, self._db)
        qs._where = list(self._where)
        qs._order = list(self._order)
        qs._limit = self._limit
        qs._offset = self._offset
        return qs

    def using(self, database) -> "QuerySet":
        qs = self._clone()
        qs._db = database
        return qs

    def _resolve_column(self, field_name: str) -> str:
        field = self.model._meta.fields.get(field_name)
        if field is None:
            # allow raw column names (e.g. FK ``author_id``)
            return field_name
        return field.column

    def _add_conditions(self, negate: bool, kwargs: dict):
        qs = self._clone()
        for key, value in kwargs.items():
            if "__" in key:
                field_name, lookup = key.rsplit("__", 1)
            else:
                field_name, lookup = key, "exact"
            if lookup not in _OPERATORS:
                field_name, lookup = key, "exact"
            column = self._resolve_column(field_name)
            op = _OPERATORS[lookup]

            if lookup == "isnull":
                frag = f'"{column}" IS {"NOT NULL" if not value else "NULL"}'
                if value in (True, False):
                    frag = f'"{column}" IS {"NULL" if value else "NOT NULL"}'
                qs._where.append((f"NOT ({frag})" if negate else frag, []))
            elif lookup == "in":
                if not value:
                    qs._where.append(("FALSE" if not negate else "TRUE", []))
                else:
                    marks = ", ".join(["?"] * len(value))
                    frag = f'"{column}" IN ({marks})'
                    qs._where.append((f"NOT ({frag})" if negate else frag, list(value)))
            else:
                v = value
                if lookup in ("contains", "icontains"):
                    v = f"%{value}%"
                elif lookup in ("startswith", "istartswith"):
                    v = f"{value}%"
                elif lookup == "endswith":
                    v = f"%{value}"
                frag = f'"{column}" {op} ?'
                qs._where.append((f"NOT ({frag})" if negate else frag, [v]))
        return qs

    def filter(self, **kwargs) -> "QuerySet":
        return self._add_conditions(False, kwargs)

    def exclude(self, **kwargs) -> "QuerySet":
        return self._add_conditions(True, kwargs)

    def order_by(self, *fields) -> "QuerySet":
        qs = self._clone()
        for f in fields:
            if f.startswith("-"):
                qs._order.append(f'"{self._resolve_column(f[1:])}" DESC')
            else:
                qs._order.append(f'"{self._resolve_column(f)}" ASC')
        return qs

    def limit(self, n: int) -> "QuerySet":
        qs = self._clone()
        qs._limit = n
        return qs

    def offset(self, n: int) -> "QuerySet":
        qs = self._clone()
        qs._offset = n
        return qs

    # -- SQL assembly (converts ``?`` placeholders to $1, $2, ...) --------------

    def _where_sql(self, start_index: int = 1):
        if not self._where:
            return "", [], start_index
        clauses, params = [], []
        idx = start_index
        for frag, frag_params in self._where:
            out = []
            for ch in frag:
                if ch == "?":
                    out.append(f"${idx}")
                    idx += 1
                else:
                    out.append(ch)
            clauses.append("".join(out))
            params.extend(frag_params)
        return " WHERE " + " AND ".join(clauses), params, idx

    def _select_sql(self):
        where_sql, params, _ = self._where_sql()
        sql = f'SELECT * FROM "{self.model._meta.table_name}"{where_sql}'
        if self._order:
            sql += " ORDER BY " + ", ".join(self._order)
        if self._limit is not None:
            sql += f" LIMIT {int(self._limit)}"
        if self._offset is not None:
            sql += f" OFFSET {int(self._offset)}"
        return sql, params

    # -- Terminal operations (async) --------------------------------------------

    async def all(self) -> list:
        db = _require_db(self._db)
        sql, params = self._select_sql()
        rows = await db.fetch(sql, *params)
        return [self.model._from_row(r) for r in rows]

    def __await__(self):
        return self.all().__await__()

    async def values(self, *fields) -> list:
        """Return matching rows as plain dicts (optionally a subset of columns)."""
        rows = await self.all()
        dicts = [r.to_dict() for r in rows]
        if fields:
            return [{k: d.get(k) for k in fields} for d in dicts]
        return dicts

    async def first(self):
        rows = await self.limit(1).all()
        return rows[0] if rows else None

    async def get(self, **kwargs):
        qs = self.filter(**kwargs) if kwargs else self
        rows = await qs.limit(2).all()
        if not rows:
            raise self.model.DoesNotExist(
                f"{self.model.__name__} matching query does not exist."
            )
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"get() returned more than one {self.model.__name__}."
            )
        return rows[0]

    async def count(self) -> int:
        db = _require_db(self._db)
        where_sql, params, _ = self._where_sql()
        sql = f'SELECT count(*) AS n FROM "{self.model._meta.table_name}"{where_sql}'
        row = await db.fetchrow(sql, *params)
        return int(row["n"])

    async def exists(self) -> bool:
        return (await self.count()) > 0

    async def delete(self) -> int:
        db = _require_db(self._db)
        where_sql, params, _ = self._where_sql()
        sql = f'DELETE FROM "{self.model._meta.table_name}"{where_sql}'
        return await db.execute(sql, *params)

    async def update(self, **kwargs) -> int:
        db = _require_db(self._db)
        meta = self.model._meta
        sets, params = [], []
        idx = 1
        for name, value in kwargs.items():
            column = self._resolve_column(name)
            sets.append(f'"{column}" = ${idx}')
            params.append(value)
            idx += 1
        where_sql, where_params, _ = self._where_sql(start_index=idx)
        params.extend(where_params)
        sql = f'UPDATE "{meta.table_name}" SET {", ".join(sets)}{where_sql}'
        return await db.execute(sql, *params)


class Manager:
    """Entry point exposed as ``Model.objects``."""

    def __init__(self, model):
        self.model = model

    def get_queryset(self, using=None) -> QuerySet:
        return QuerySet(self.model, using)

    def using(self, database) -> QuerySet:
        return self.get_queryset(database)

    def all(self) -> QuerySet:
        return self.get_queryset()

    def filter(self, **kwargs) -> QuerySet:
        return self.get_queryset().filter(**kwargs)

    def exclude(self, **kwargs) -> QuerySet:
        return self.get_queryset().exclude(**kwargs)

    def order_by(self, *fields) -> QuerySet:
        return self.get_queryset().order_by(*fields)

    async def get(self, **kwargs):
        return await self.get_queryset().get(**kwargs)

    async def create(self, using=None, **kwargs):
        """Insert a row and return the populated model instance."""
        obj = self.model(**kwargs)
        return await obj.save(using=using)

    async def count(self) -> int:
        return await self.get_queryset().count()
