# Learnings

Debugging surprises, non-obvious gotchas, and operational lessons. Complements ADRs
(which capture *decisions*) by capturing *things that burned time* so they don't repeat.

---

## L001 — Introducing Alembic to a live database bootstrapped with `create_all`

**Date:** 2026-04-08  
**Area:** Database migrations / Alembic

### What happened

The project originally used `Base.metadata.create_all()` in `init_db()` to create tables at
startup. When Alembic was introduced / extended later, running `alembic upgrade head` failed
because the tables already existed, and the `alembic_version` tracking table did not.

Attempting to stamp the existing DB via `docker compose run` also failed — the container image
was stale (built before the `env.py` change was committed), so `DATABASE_URL` override had
no effect.

A secondary failure: manually creating `alembic_version` as the `postgres` superuser meant
the app user (`portfolio_user`) got `permission denied` on the next startup.

### Root causes

1. **`docker compose restart` reuses the existing image.** Code changes only take effect after
   `docker compose up -d --build`. Restart ≠ rebuild.
2. **`docker compose run` may use a stale image** if not rebuilt first.
3. **Tables bootstrapped via `create_all` are invisible to Alembic.** The DB must be stamped
   at the correct revision before Alembic can take over.
4. **Manually created DB objects inherit the creating role's ownership.** Run all DDL as the
   application user, or grant immediately after creation.

### Fix (one-time for existing DBs)

```sql
-- Run as postgres superuser in the target database
CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('002') ON CONFLICT DO NOTHING;
GRANT ALL ON TABLE alembic_version TO portfolio_user;
```

Then `docker compose up -d --build <service>` — Alembic will find `002` and run no migrations.

### Prevention going forward

- **New projects:** Add Alembic from the start. Never use `create_all` in production code paths.
- **Existing projects:** When adding Alembic mid-life, stamp via SQL first, then rebuild.
- **Always `--build`** when deploying code changes: `docker compose up -d --build <service>`.
- **Grant to app user immediately** when creating any table manually.
- **Use `docker exec <running-container>`** rather than `docker compose run` to get the exact
  live environment, including already-resolved env vars.

---

## L003 — Stamping Alembic at a revision does NOT run the migration's DDL

**Date:** 2026-04-08  
**Area:** Database migrations / Alembic

### What happened

When adding Alembic mid-life (after tables were bootstrapped via `create_all`), the DB was
stamped at revision `002` via SQL:

```sql
INSERT INTO alembic_version VALUES ('002');
```

This told Alembic "you are already at 002". On the next `alembic upgrade head`, it found no
pending migrations and exited cleanly — but the actual DDL in migration 002 (adding
`input_tokens`, `output_tokens`, `cost_usd` columns) never ran against the live database.

The columns were absent, so token data was silently dropped on every write. The service
started without errors because SQLAlchemy writes are non-strict about missing nullable columns
in shadow mode, and the tool caught no exception.

### Root cause

`alembic stamp` marks the DB as *already at* a revision. It does not execute the migration.
If you stamp at `002` but the DB was bootstrapped before `002` existed, the `002` DDL is
permanently skipped.

### Fix

Run the migration DDL manually, then the stamp is accurate:

```sql
ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;
```

Because the migration uses `ADD COLUMN IF NOT EXISTS`, this is idempotent — safe to run
whether or not Alembic later runs the migration itself.

### Prevention going forward

- When stamping an existing DB at revision N, always verify the DB actually has the schema
  that revision N describes (check `\d table_name`).
- If columns from revision N are missing, run the DDL manually before stamping.
- Alternatively: stamp at the revision *before* the missing one (`001`), then run
  `alembic upgrade head` — this executes the real migration.

---

## L004 — `from module import name` captures `None` at import time

**Date:** 2026-04-08  
**Area:** Python / FastAPI

### What happened

`dashboard.py` imported `AsyncSessionLocal` directly:

```python
from app.db.session import AsyncSessionLocal
```

`AsyncSessionLocal` starts as `None` at module level and is assigned inside `init_db()`,
which runs during the FastAPI lifespan. By the time `init_db()` set it, the dashboard
module already held a reference to the original `None` — the binding never updated.

Every request to `/api/dashboard/stats` raised `TypeError: 'NoneType' object is not callable`.

### Root cause

`from module import name` binds to the *value* of `name` at import time, not to the
module attribute itself. Reassigning the module-level variable later does not update
existing references in other modules.

### Fix

Import the module and access the attribute at call time:

```python
import app.db.session as _db_session
# ...
async with _db_session.AsyncSessionLocal() as session:
```

This dereferences the attribute each time, always getting the current value.

### Prevention going forward

Any module-level variable that is `None` at import and set later (lazy init, lifespan,
dependency injection) must be accessed via the module reference, not a direct import.

---

## L005 — FastAPI `Form(...)` parameter consumes the request stream

**Date:** 2026-04-08  
**Area:** FastAPI / Slack integration

### What happened

The Slack action handler used `payload: str = Form(...)` as a function parameter so
FastAPI would parse the URL-encoded body automatically. The handler also called
`await request.body()` to verify the Slack signing signature. This raised:

```
RuntimeError: Stream consumed
```

### Root cause

When FastAPI sees a `Form(...)` parameter, it reads and parses the entire request body
before the handler function executes. The underlying stream can only be read once, so
the subsequent `await request.body()` call finds nothing left and raises.

### Fix

Read the raw body manually first, then parse the form fields:

```python
body = await request.body()
# verify signature against body...
from urllib.parse import parse_qs
form = parse_qs(body.decode("utf-8"))
payload_raw = form.get("payload", [None])[0]
```

Remove `Form(...)` from the function signature entirely.

### Prevention going forward

Never mix `Form(...)` parameters with `await request.body()` in the same handler.
If you need raw body access (for HMAC verification), always read the body manually
and parse it yourself.

---

## L002 — `docker compose run -e KEY=value` is shadowed by service environment

**Date:** 2026-04-08  
**Area:** Docker / deployment

### What happened

Passing `-e DATABASE_URL=...` to `docker compose run` appeared to have no effect — Alembic
still connected to `localhost` (the alembic.ini default) rather than the Postgres container.

### Root cause

`docker compose run -e KEY=value` merges with the service's `environment:` block. If `KEY`
is already defined there (or resolved from `.env`), the service definition wins over the
explicit `-e` flag.

### Fix

Use `docker exec <already-running-container> <command>` instead — this inherits the live
service environment directly with no merging surprises.
