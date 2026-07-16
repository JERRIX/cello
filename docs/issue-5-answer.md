# Answer to issue #5 — Database pool & Redis client in startup hooks and handlers

Thanks for the detailed write-up, and for catching this. You were right to be
suspicious: in **v1.2.0 the database/Redis layer was mock scaffolding** — the
methods returned placeholder values (`[]`, `None`, `True`) and never connected to
a server. That's why the only example used `mock_users`/`mock_cache`. Your code
didn't work because there was nothing real underneath it, not because you were
holding it wrong.

**This is now fixed.** Cello ships a real native data layer (Postgres via
`deadpool-postgres`, Redis via the `redis` crate), plus a small built-in
ORM. Everything below is tested end-to-end against real PostgreSQL + Redis.

---

## The corrected minimal example

```python
import json
from cello import App, DatabaseConfig, RedisConfig, Response
from cello.database import transactional

app = App()
app.enable_database(DatabaseConfig(url="postgresql://user:password@localhost:5432/mydb", pool_size=10))
app.enable_redis(RedisConfig(url="redis://localhost:6379"))

@app.on_event("startup")
async def startup():
    # app.database and app.redis are the live pools — use them here.
    await app.database.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id    SERIAL PRIMARY KEY,
            name  TEXT NOT NULL,
            email TEXT
        )
    """)
    await app.redis.ping()
    await app.redis.set("app:status", "ready")

@app.on_event("shutdown")
async def shutdown():
    await app.database.close()
    await app.redis.close()

@app.get("/users")
async def get_users(request):
    cached = await request.redis.get("users:all")
    if cached:
        return {"users": json.loads(cached), "source": "cache"}
    rows = await request.database.fetch("SELECT * FROM users ORDER BY id")  # -> list[dict]
    await request.redis.set("users:all", json.dumps(rows), ex=60)
    return {"users": rows, "source": "db"}

@app.post("/users")
async def create_user(request):
    data = request.json()
    new_id = await request.database.fetchval(
        "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
        data["name"], data.get("email"),
    )
    await request.redis.delete("users:all")
    return Response.json({"created": True, "id": new_id}, status=201)

@app.post("/transfer")
@transactional
async def transfer(request, tx):                      # tx is injected by @transactional
    data = request.json()
    await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", data["amount"], data["from"])
    await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2", data["amount"], data["to"])
    return {"ok": True}                                # commits here; any exception rolls back
```

---

## Your specific questions, answered

### Database

1. **Access the pool in `startup`?** → `app.database`. (Not `app.db`/`app.pool`.)
2. **Run `CREATE TABLE` in `startup`?** → `await app.database.execute("CREATE TABLE ...")`.
3. **Access it in handlers?** → `request.database` (alias `request.db`).
4. **Query methods?** → `fetch(sql, *params) -> list[dict]`, `fetchrow(sql, *params) -> dict | None`,
   `fetchval(sql, *params) -> scalar`, `execute(sql, *params) -> rows_affected (int)`. Params are
   positional `$1, $2, …` (asyncpg-style), always parameterized — never string-format SQL.
5. **Explicit `acquire()`?** → No. Call `fetch`/`execute` directly on the pool (like asyncpg's
   `pool.fetch`). For a multi-statement unit of work, use `async with request.database.transaction() as tx:`.
6. **Transactional connection?** → Either `@transactional` (injects a `tx` argument), or
   `async with request.database.transaction() as tx: await tx.execute(...)`. Commit is automatic on
   success, rollback on exception.
7. **Close the pool?** → `await app.database.close()` in `shutdown`.

### Redis

8. **Access in `startup`?** → `app.redis`. In handlers → `request.redis`.
9. **Commands available?** → `get/set(key, value, ex=…, nx=…)/setex/delete/exists/expire/ttl/`
   `incr/incrby/decr/decrby/mget/keys/hget/hset/hgetall/hdel/lpush/rpush/lpop/rpop/lrange/llen/`
   `sadd/srem/smembers/sismember/publish/eval/evalsha/script_load/ping/dbsize/flushdb`. Your
   `script_load` + `evalsha` snippet works as written.
10. **Use `request.redis` inside a `@transactional` handler?** → Yes — Redis and the DB transaction
    are independent; mix them freely.
11. **Close the connection?** → `await app.redis.close()` in `shutdown`.

### Driver

12. **Underlying driver?** → **Rust**, not asyncpg/psycopg. Postgres = `deadpool-postgres` +
    `tokio-postgres`; Redis = the `redis` crate's async `ConnectionManager`. The API is modelled on
    asyncpg for familiarity, and the GIL is released for the whole DB/Redis round-trip.

---

## Issues in your original code

- `db = app.database` — **correct now** (previously there was no such attribute).
- `async with db.acquire() as conn:` — **there is no `acquire()`**. Call `await db.fetch(...)` /
  `db.execute(...)` directly, or use `db.transaction()` for a transaction.
- `r = app.redis` — **correct now**.
- `@transactional` with `conn = ???` — the connection is provided for you: declare your handler as
  `async def create_user(request, tx)` and use `tx.execute(...)`.
- `await request.redis.set("users:all", result, ex=60)` — pass a **string** (e.g.
  `json.dumps(result)`); values are stored as bytes, not auto-serialized Python objects.

---

## Bonus: the new ORM (if you prefer models over raw SQL)

```python
from cello.orm import Model, AutoField, CharField, BooleanField, setup

class User(Model):
    id = AutoField()
    name = CharField(max_length=100)
    email = CharField(max_length=255, null=True)
    active = BooleanField(default=True)

@app.on_event("startup")
async def startup():
    setup(app.database)            # bind the ORM to the pool
    await User.create_table()

@app.get("/users")
async def users(request):
    rows = await User.objects.filter(active=True).order_by("-id").limit(20)
    return {"users": [u.to_dict() for u in rows]}

@app.post("/users")
async def create(request):
    data = request.json()
    user = await User.objects.create(name=data["name"], email=data.get("email"))
    return user.to_dict()
```

Supported: typed fields (`AutoField/IntegerField/CharField/TextField/BooleanField/FloatField/`
`JSONField/DateTimeField/ForeignKey`), chainable `filter/exclude/order_by/limit/offset` with field
lookups (`__gt/__gte/__lt/__lte/__in/__contains/__icontains/__startswith/__isnull`), and
`get/first/all/count/exists/values/create/update/delete`, plus `create_table`/`drop_table` and
`ForeignKey`.

**Honest scope:** this is intentionally the common-80% ORM. It does **not** include migration
autogeneration/diffing, lazy reverse relations, a `select_related` join planner, or signals/admin.
For those, use raw `request.database` queries (they cover everything).

A full runnable example is in `examples/database_orm_demo.py`.
