"""
Cello Database & Redis helpers.

Since v1.3.0 the database and Redis integrations are **native and real**: the
connection pools live in Rust (``deadpool-postgres`` and the ``redis`` crate) and
are reached through ``app.database`` / ``request.database`` and ``app.redis`` /
``request.redis``. The ``Database``, ``Transaction`` and ``Redis`` types are the
native classes exported from the compiled extension — import them from ``cello``
if you need them for type hints::

    from cello import App, DatabaseConfig, RedisConfig
    from cello.database import transactional

    app = App()
    app.enable_database(DatabaseConfig(url="postgresql://user:pass@localhost/db"))
    app.enable_redis(RedisConfig(url="redis://localhost:6379"))

    @app.on_event("startup")
    async def startup():
        await app.database.execute("CREATE TABLE IF NOT EXISTS users(id serial primary key, name text)")

    @app.get("/users")
    async def users(request):
        return {"users": await request.database.fetch("SELECT * FROM users")}

This module now provides just the :func:`transactional` decorator; the query and
Redis methods are documented on the native ``Database`` / ``Redis`` classes.
"""

from functools import wraps
from typing import Callable

__all__ = ["transactional"]


def transactional(func: Callable) -> Callable:
    """
    Run an async handler inside a database transaction.

    The handler must be ``async`` and is called with an extra ``tx`` keyword
    argument — a native transaction bound to a single pooled connection. The
    transaction commits when the handler returns normally and rolls back if it
    raises.

    Example::

        @app.post("/transfer")
        @transactional
        async def transfer(request, tx):
            data = request.json()
            await tx.execute(
                "UPDATE accounts SET balance = balance - $1 WHERE id = $2",
                data["amount"], data["from"],
            )
            await tx.execute(
                "UPDATE accounts SET balance = balance + $1 WHERE id = $2",
                data["amount"], data["to"],
            )
            return {"ok": True}

    Requires ``app.enable_database(...)``; ``request.database`` must be available
    (it is injected automatically for every route once the database is enabled).
    """
    import inspect

    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            "@transactional requires an 'async def' handler; native transactions "
            "are asynchronous."
        )

    @wraps(func)
    async def wrapper(request, *args, **kwargs):
        try:
            database = request.database
        except AttributeError as exc:
            raise RuntimeError(
                "@transactional needs a database. Call app.enable_database(...) first."
            ) from exc
        async with database.transaction() as tx:
            return await func(request, *args, tx=tx, **kwargs)

    return wrapper
