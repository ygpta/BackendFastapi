# Schema Migrations

Run `uv run alembic upgrade head` before starting API or worker replicas.
Review generated changes with `uv run alembic check`; do not edit deployed revisions.

Application tables are registered by importing `backendfastapi.db.models`.
Their domain modules share the same metadata in `backendfastapi.db.base`.
Moving Python model files does not require a migration when table definitions stay identical.

LangGraph checkpoint tables are managed separately by
`python -m backendfastapi.worker --setup`; Alembic deliberately excludes them.
Compose runs migration and checkpoint setup as one-shot jobs, not in each API worker.

Integration tests require `TEST_DATABASE_URL` pointing to a disposable PostgreSQL
database. They downgrade and rebuild the schema; never use an application database.