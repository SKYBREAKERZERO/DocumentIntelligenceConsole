"""Destructively clear only 21MM/24MM SQL data and their Qdrant collections.

Schema/migration tables, COMMON, QABUG, local configuration and source files are
preserved. Execution requires an exact confirmation token.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg
from psycopg import sql

from app.enterprise.qdrant_service import QdrantService
from app.server.configuration import load_profiles, load_settings, user_configuration_dir


CONFIRM = "CLEAR_21MM_24MM"
SCOPES = ("21MM", "24MM")
ALLOWED_DATABASES = {"21MM": "rag_21mm", "24MM": "rag_24mm"}
PRESERVE_TABLES = {"schema_version", "schema_migrations", "alembic_version"}


def public_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename")
        return [str(row[0]) for row in cur.fetchall()]


def has_rows(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(sql.Identifier("public", table)))
        return bool(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    config_dir = user_configuration_dir()
    profiles, password, qprofile, qkey = load_profiles(config_dir)
    settings = load_settings(config_dir)

    for scope in SCOPES:
        actual = profiles[scope].database
        if actual != ALLOWED_DATABASES[scope]:
            raise RuntimeError(f"Refusing unexpected database for {scope}: {actual!r}")

    qdrant = QdrantService(qprofile, qkey)
    existing = set(qdrant.test_connection())
    configured = {str(settings.collections[scope]) for scope in SCOPES}
    acceptance = {name for name in existing if name.startswith("dic_accept_") and name.lower().endswith(("_21mm", "_24mm"))}
    qtargets = sorted((configured | acceptance) & existing)

    print("SQL targets:", ", ".join(ALLOWED_DATABASES.values()))
    print("Qdrant targets:", ", ".join(qtargets) if qtargets else "(none present)")
    if args.confirm != CONFIRM:
        print(f"Dry run only. Execute with --confirm {CONFIRM}")
        return 0

    for scope in SCOPES:
        profile = profiles[scope]
        kwargs = profile.dsn_kwargs()
        kwargs.update(password=password, options="-c statement_timeout=0 -c lock_timeout=10000")
        with psycopg.connect(**kwargs) as conn:
            tables = [name for name in public_tables(conn) if name not in PRESERVE_TABLES]
            if tables:
                statement = sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier("public", name) for name in tables)
                )
                with conn.cursor() as cur:
                    cur.execute(statement)
            conn.commit()
            remaining = [name for name in tables if has_rows(conn, name)]
            if remaining:
                raise RuntimeError(f"{profile.database} still has rows: {remaining}")
            print(f"CLEARED SQL {scope}: database={profile.database}; tables={len(tables)}; remaining=0")

    for name in qtargets:
        qdrant.delete_collection(name)
        if qdrant.collection_exists(name):
            raise RuntimeError(f"Qdrant collection still exists: {name}")
        print(f"DELETED QDRANT: {name}")

    untouched = sorted(set(qdrant.test_connection()))
    print("UNTOUCHED QDRANT:", ", ".join(untouched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
