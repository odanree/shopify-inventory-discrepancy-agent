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
