from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.enterprise.profiles import PostgresProfile


READ_PREFIXES = {
    "SELECT",
    "WITH",
    "SHOW",
    "EXPLAIN",
    "VALUES",
    "TABLE",
}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    rowcount: int
    elapsed_ms: float
    command: str
    total_rows: int | None = None


class PostgresService:
    """PostgreSQL metadata, SQL, and PK-safe CRUD service."""

    def __init__(self, profile: PostgresProfile, password: str) -> None:
        self.profile = profile
        self.password = password

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        kwargs = self.profile.dsn_kwargs()
        kwargs["password"] = self.password
        return psycopg.connect(**kwargs, row_factory=dict_row)

    def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        current_database() AS database,
                        current_user AS user_name,
                        current_schema AS current_schema,
                        version() AS version,
                        inet_server_addr()::text AS server_addr,
                        inet_server_port() AS server_port
                    """
                )
                row = cur.fetchone() or {}
        return {
            **row,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    # ------------------------------------------------------------------
    # Database metadata
    # ------------------------------------------------------------------

    def list_schemas(self) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
                      AND schema_name NOT LIKE 'pg_toast%'
                      AND schema_name NOT LIKE 'pg_temp%'
                    ORDER BY schema_name
                    """
                )
                return [row["schema_name"] for row in cur.fetchall()]

    def list_objects(self, schema: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name AS object_name,
                           CASE table_type
                               WHEN 'BASE TABLE' THEN 'TABLE'
                               WHEN 'VIEW' THEN 'VIEW'
                               ELSE table_type
                           END AS object_type
                    FROM information_schema.tables
                    WHERE table_schema = %s
                    UNION ALL
                    SELECT matviewname AS object_name, 'MATERIALIZED VIEW' AS object_type
                    FROM pg_matviews
                    WHERE schemaname = %s
                    UNION ALL
                    SELECT sequence_name AS object_name, 'SEQUENCE' AS object_type
                    FROM information_schema.sequences
                    WHERE sequence_schema = %s
                    ORDER BY object_type, object_name
                    """,
                    (schema, schema, schema),
                )
                return list(cur.fetchall())

    def metadata_snapshot(self) -> dict[str, Any]:
        """Load connection info, schemas, and database objects in one session.

        The unified console uses this for Database Explorer so a refresh does
        not create several short-lived PostgreSQL connections back-to-back.
        This is faster on Windows and materially reduces the chance that a
        metadata page appears to remain at "Connecting…" on slow systems.
        """
        started = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        current_database() AS database,
                        current_user AS user_name,
                        current_schema AS current_schema,
                        version() AS version,
                        inet_server_addr()::text AS server_addr,
                        inet_server_port() AS server_port
                    """
                )
                info = cur.fetchone() or {}

                cur.execute(
                    """
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
                      AND schema_name NOT LIKE 'pg_toast%'
                      AND schema_name NOT LIKE 'pg_temp%'
                    ORDER BY schema_name
                    """
                )
                schemas = [row["schema_name"] for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT table_schema AS schema_name,
                           table_name AS object_name,
                           CASE table_type
                               WHEN 'BASE TABLE' THEN 'TABLE'
                               WHEN 'VIEW' THEN 'VIEW'
                               ELSE table_type
                           END AS object_type
                    FROM information_schema.tables
                    WHERE table_schema <> 'information_schema'
                      AND table_schema NOT LIKE 'pg_%'
                    UNION ALL
                    SELECT schemaname AS schema_name,
                           matviewname AS object_name,
                           'MATERIALIZED VIEW' AS object_type
                    FROM pg_matviews
                    WHERE schemaname NOT LIKE 'pg_%'
                    UNION ALL
                    SELECT sequence_schema AS schema_name,
                           sequence_name AS object_name,
                           'SEQUENCE' AS object_type
                    FROM information_schema.sequences
                    WHERE sequence_schema <> 'information_schema'
                      AND sequence_schema NOT LIKE 'pg_%'
                    ORDER BY schema_name, object_type, object_name
                    """
                )
                objects_by_schema: dict[str, list[dict[str, Any]]] = {
                    name: [] for name in schemas
                }
                for row in cur.fetchall():
                    schema_name = str(row["schema_name"])
                    if schema_name in objects_by_schema:
                        objects_by_schema[schema_name].append(
                            {
                                "object_name": row["object_name"],
                                "object_type": row["object_type"],
                            }
                        )

        return {
            "connection": {
                **info,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            "schemas": objects_by_schema,
        }

    def list_tables(self, schema: str) -> list[dict[str, Any]]:
        return [
            {"table_name": item["object_name"], "table_type": item["object_type"]}
            for item in self.list_objects(schema)
            if item["object_type"] in {"TABLE", "VIEW", "MATERIALIZED VIEW"}
        ]

    def table_columns(self, schema: str, table: str) -> list[dict[str, Any]]:
        pk = set(self.primary_key_columns(schema, table))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.ordinal_position,
                        c.column_name,
                        c.data_type,
                        c.udt_name,
                        c.is_nullable,
                        c.column_default,
                        c.character_maximum_length,
                        c.numeric_precision,
                        c.numeric_scale,
                        c.is_identity,
                        c.identity_generation,
                        c.is_generated,
                        c.generation_expression
                    FROM information_schema.columns c
                    WHERE c.table_schema = %s
                      AND c.table_name = %s
                    ORDER BY c.ordinal_position
                    """,
                    (schema, table),
                )
                rows = list(cur.fetchall())
        for row in rows:
            row["is_primary_key"] = row["column_name"] in pk
        return rows

    def table_indexes(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        i.indexname,
                        i.indexdef,
                        COALESCE(x.indisunique, false) AS is_unique,
                        COALESCE(x.indisprimary, false) AS is_primary
                    FROM pg_indexes i
                    LEFT JOIN pg_namespace n
                      ON n.nspname = i.schemaname
                    LEFT JOIN pg_class t
                      ON t.relnamespace = n.oid
                     AND t.relname = i.tablename
                    LEFT JOIN pg_class idx
                      ON idx.relnamespace = n.oid
                     AND idx.relname = i.indexname
                    LEFT JOIN pg_index x
                      ON x.indrelid = t.oid
                     AND x.indexrelid = idx.oid
                    WHERE i.schemaname = %s
                      AND i.tablename = %s
                    ORDER BY i.indexname
                    """,
                    (schema, table),
                )
                return list(cur.fetchall())

    def table_constraints(self, schema: str, table: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        tc.constraint_name,
                        tc.constraint_type,
                        kcu.column_name,
                        kcu.ordinal_position,
                        ccu.table_schema AS foreign_table_schema,
                        ccu.table_name AS foreign_table_name,
                        ccu.column_name AS foreign_column_name
                    FROM information_schema.table_constraints tc
                    LEFT JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    LEFT JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.table_schema = %s
                      AND tc.table_name = %s
                    ORDER BY tc.constraint_type, tc.constraint_name, kcu.ordinal_position
                    """,
                    (schema, table),
                )
                return list(cur.fetchall())

    def primary_key_columns(self, schema: str, table: str) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.table_schema = %s
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY kcu.ordinal_position
                    """,
                    (schema, table),
                )
                return [row["column_name"] for row in cur.fetchall()]

    def foreign_keys(self, schema: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        predicate = ""
        if schema:
            predicate = "AND tc.table_schema = %s"
            params.append(schema)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        tc.constraint_name,
                        tc.table_schema AS source_schema,
                        tc.table_name AS source_table,
                        kcu.column_name AS source_column,
                        ccu.table_schema AS target_schema,
                        ccu.table_name AS target_table,
                        ccu.column_name AS target_column,
                        kcu.ordinal_position
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                    {predicate}
                    ORDER BY source_schema, source_table, constraint_name, kcu.ordinal_position
                    """,
                    params,
                )
                return list(cur.fetchall())

    def schema_erd_model(self, schema: str) -> dict[str, Any]:
        """Build an ERD model with one PostgreSQL connection.

        v5.0.1 built the model by opening multiple connections per table
        (table list, PK lookup, column lookup, FK lookup).  The enterprise
        console now performs the complete metadata scan in one read-only
        session so ERD loading is predictable even with many tables.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """,
                    (schema,),
                )
                tables = [str(row["table_name"]) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        c.table_name,
                        c.ordinal_position,
                        c.column_name,
                        c.data_type,
                        c.is_nullable
                    FROM information_schema.columns c
                    WHERE c.table_schema = %s
                    ORDER BY c.table_name, c.ordinal_position
                    """,
                    (schema,),
                )
                column_rows = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT
                        tc.table_name,
                        kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.table_schema = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY tc.table_name, kcu.ordinal_position
                    """,
                    (schema,),
                )
                pk_rows = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT
                        tc.constraint_name,
                        tc.table_schema AS source_schema,
                        tc.table_name AS source_table,
                        kcu.column_name AS source_column,
                        ccu.table_schema AS target_schema,
                        ccu.table_name AS target_table,
                        ccu.column_name AS target_column,
                        kcu.ordinal_position
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = %s
                    ORDER BY source_table, constraint_name, kcu.ordinal_position
                    """,
                    (schema,),
                )
                foreign_keys = list(cur.fetchall())

        primary_keys: set[tuple[str, str]] = {
            (str(row["table_name"]), str(row["column_name"]))
            for row in pk_rows
        }
        by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in tables}
        for row in column_rows:
            table = str(row["table_name"])
            if table not in by_table:
                continue
            name = str(row["column_name"])
            by_table[table].append(
                {
                    "name": name,
                    "type": row["data_type"],
                    "pk": (table, name) in primary_keys,
                    "nullable": row["is_nullable"] == "YES",
                }
            )

        models = [
            {"schema": schema, "table": table, "columns": by_table[table]}
            for table in tables
        ]
        return {"tables": models, "foreign_keys": foreign_keys}

    # ------------------------------------------------------------------
    # Data browsing
    # ------------------------------------------------------------------

    def table_count(self, schema: str, table: str, filter_text: str = "") -> int:
        columns = self.table_columns(schema, table)
        query = sql.SQL("SELECT COUNT(*) AS count FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        params: list[Any] = []
        if filter_text.strip():
            text_columns = [
                c["column_name"]
                for c in columns
                if c["data_type"]
                in {
                    "text",
                    "character varying",
                    "character",
                    "uuid",
                    "json",
                    "jsonb",
                    "citext",
                }
            ]
            if text_columns:
                predicates = [
                    sql.SQL("CAST({} AS TEXT) ILIKE %s").format(sql.Identifier(name))
                    for name in text_columns
                ]
                query += sql.SQL(" WHERE ") + sql.SQL(" OR ").join(predicates)
                params.extend([f"%{filter_text.strip()}%"] * len(predicates))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone() or {"count": 0}
                return int(row["count"])

    def fetch_table_rows(
        self,
        schema: str,
        table: str,
        *,
        limit: int,
        offset: int,
        filter_text: str = "",
        sort_column: str = "",
        sort_direction: str = "ASC",
    ) -> QueryResult:
        started = time.perf_counter()
        columns = self.table_columns(schema, table)
        column_names = [c["column_name"] for c in columns]

        query = sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        params: list[Any] = []

        if filter_text.strip():
            text_columns = [
                c["column_name"]
                for c in columns
                if c["data_type"]
                in {
                    "text",
                    "character varying",
                    "character",
                    "uuid",
                    "json",
                    "jsonb",
                    "citext",
                }
            ]
            if text_columns:
                predicates = [
                    sql.SQL("CAST({} AS TEXT) ILIKE %s").format(sql.Identifier(name))
                    for name in text_columns
                ]
                query += sql.SQL(" WHERE ") + sql.SQL(" OR ").join(predicates)
                params.extend([f"%{filter_text.strip()}%"] * len(predicates))

        sort_direction = sort_direction.upper()
        if sort_direction not in {"ASC", "DESC"}:
            sort_direction = "ASC"

        if sort_column and sort_column in column_names:
            query += sql.SQL(" ORDER BY {} {}").format(
                sql.Identifier(sort_column), sql.SQL(sort_direction)
            )
        else:
            pk_columns = self.primary_key_columns(schema, table)
            if pk_columns:
                query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                    sql.Identifier(name) for name in pk_columns
                )

        query += sql.SQL(" LIMIT %s OFFSET %s")
        params.extend([int(limit), int(offset)])

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = list(cur.fetchall())

        total = self.table_count(schema, table, filter_text)
        return QueryResult(
            columns=column_names,
            rows=rows,
            rowcount=len(rows),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            command="SELECT",
            total_rows=total,
        )

    # ------------------------------------------------------------------
    # PK-safe CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pk_predicate(pk_values: dict[str, Any]) -> tuple[sql.Composed, list[Any]]:
        if not pk_values:
            raise ValueError("Primary-key values are required for safe row mutation.")
        predicates = []
        params: list[Any] = []
        for name, value in pk_values.items():
            if value is None:
                predicates.append(sql.SQL("{} IS NULL").format(sql.Identifier(name)))
            else:
                predicates.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
                params.append(value)
        return sql.SQL(" AND ").join(predicates), params

    def insert_row(self, schema: str, table: str, values: dict[str, Any]) -> dict[str, Any]:
        columns = self.table_columns(schema, table)
        allowed = {c["column_name"] for c in columns}
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"Unknown columns: {', '.join(sorted(invalid))}")

        with self._connect() as conn:
            with conn.cursor() as cur:
                if values:
                    names = list(values)
                    query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) RETURNING *").format(
                        sql.Identifier(schema),
                        sql.Identifier(table),
                        sql.SQL(", ").join(sql.Identifier(name) for name in names),
                        sql.SQL(", ").join(sql.Placeholder() for _ in names),
                    )
                    cur.execute(query, [values[name] for name in names])
                else:
                    query = sql.SQL("INSERT INTO {}.{} DEFAULT VALUES RETURNING *").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                    cur.execute(query)
                row = cur.fetchone() or {}
            conn.commit()
        return dict(row)

    def update_row(
        self,
        schema: str,
        table: str,
        *,
        pk_values: dict[str, Any],
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        pk_columns = self.primary_key_columns(schema, table)
        if not pk_columns:
            raise ValueError("UPDATE is disabled because this table has no primary key.")
        missing = [name for name in pk_columns if name not in pk_values]
        if missing:
            raise ValueError(f"Missing primary-key values: {', '.join(missing)}")
        if not changes:
            raise ValueError("No changed values were supplied.")
        if set(changes).intersection(pk_columns):
            raise ValueError("Primary-key columns cannot be edited in safe mode.")

        allowed = {c["column_name"] for c in self.table_columns(schema, table)}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unknown columns: {', '.join(sorted(invalid))}")

        assignments = [
            sql.SQL("{} = %s").format(sql.Identifier(name)) for name in changes
        ]
        where_sql, where_params = self._build_pk_predicate(
            {name: pk_values[name] for name in pk_columns}
        )
        query = sql.SQL("UPDATE {}.{} SET {} WHERE {} RETURNING *").format(
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.SQL(", ").join(assignments),
            where_sql,
        )
        params = list(changes.values()) + where_params
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                if row is None:
                    raise LookupError("The row no longer exists or its primary key changed.")
            conn.commit()
        return dict(row)

    def delete_rows(
        self,
        schema: str,
        table: str,
        *,
        pk_rows: Iterable[dict[str, Any]],
    ) -> int:
        pk_columns = self.primary_key_columns(schema, table)
        if not pk_columns:
            raise ValueError("DELETE is disabled because this table has no primary key.")

        rows = list(pk_rows)
        if not rows:
            return 0

        deleted = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for pk_values in rows:
                    missing = [name for name in pk_columns if name not in pk_values]
                    if missing:
                        raise ValueError(f"Missing primary-key values: {', '.join(missing)}")
                    where_sql, params = self._build_pk_predicate(
                        {name: pk_values[name] for name in pk_columns}
                    )
                    query = sql.SQL("DELETE FROM {}.{} WHERE {}").format(
                        sql.Identifier(schema), sql.Identifier(table), where_sql
                    )
                    cur.execute(query, params)
                    deleted += max(int(cur.rowcount or 0), 0)
            conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # SQL console / EXPLAIN
    # ------------------------------------------------------------------

    @staticmethod
    def _first_keyword(statement: str) -> str:
        cleaned = re.sub(
            r"(?s)^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*",
            "",
            statement,
        )
        match = re.match(r"([A-Za-z]+)", cleaned)
        return match.group(1).upper() if match else ""

    @classmethod
    def is_read_only_sql(cls, statement: str) -> bool:
        return cls._first_keyword(statement) in READ_PREFIXES

    def execute_sql(
        self,
        statement: str,
        *,
        allow_write: bool = False,
        max_rows: int = 5000,
    ) -> QueryResult:
        statement = statement.strip()
        if not statement:
            raise ValueError("SQL statement is empty.")

        keyword = self._first_keyword(statement)
        if not allow_write and keyword not in READ_PREFIXES:
            raise PermissionError(
                f"Write SQL is disabled. Command '{keyword or 'UNKNOWN'}' was blocked."
            )

        started = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                if not allow_write:
                    # Database-enforced protection: even sophisticated CTEs or
                    # side-effecting SQL cannot mutate permanent tables.
                    cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(statement)
                if cur.description:
                    columns = [desc.name for desc in cur.description]
                    rows = list(cur.fetchmany(max_rows))
                    rowcount = len(rows)
                else:
                    columns = []
                    rows = []
                    rowcount = cur.rowcount

                if allow_write and keyword not in READ_PREFIXES:
                    conn.commit()
                else:
                    conn.rollback()

        return QueryResult(
            columns=columns,
            rows=rows,
            rowcount=rowcount,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            command=keyword or "UNKNOWN",
        )

    def explain_json(self, statement: str, *, analyze: bool = False) -> dict[str, Any]:
        statement = statement.strip().rstrip(";")
        if not statement:
            raise ValueError("SQL statement is empty.")
        keyword = self._first_keyword(statement)
        if analyze and keyword != "SELECT":
            raise PermissionError("EXPLAIN ANALYZE is restricted to statements beginning with SELECT.")
        options = "ANALYZE TRUE, BUFFERS TRUE, FORMAT JSON" if analyze else "FORMAT JSON"
        explain_sql = f"EXPLAIN ({options}) {statement}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                # EXPLAIN ANALYZE executes the query, so keep the transaction
                # read-only even after the UI confirmation.
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(explain_sql)
                row = cur.fetchone()
            conn.rollback()
        if not row:
            return {}
        # psycopg may decode JSON into Python automatically.
        value = next(iter(row.values()))
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, list) and value:
            return dict(value[0])
        return dict(value or {})

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    @staticmethod
    def _quote_ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def table_ddl(self, schema: str, table: str) -> str:
        columns = self.table_columns(schema, table)
        constraints = self.table_constraints(schema, table)
        indexes = self.table_indexes(schema, table)

        lines: list[str] = []
        for col in columns:
            name = self._quote_ident(col["column_name"])
            data_type = col["data_type"]
            if data_type == "ARRAY":
                data_type = col["udt_name"]
            elif col.get("character_maximum_length") and data_type in {
                "character varying",
                "character",
            }:
                data_type = f"{data_type}({col['character_maximum_length']})"
            elif col.get("numeric_precision") and data_type in {"numeric", "decimal"}:
                if col.get("numeric_scale") is not None:
                    data_type = f"{data_type}({col['numeric_precision']},{col['numeric_scale']})"
            parts = [name, data_type]
            if col.get("is_identity") == "YES":
                parts.append(f"GENERATED {col.get('identity_generation') or 'BY DEFAULT'} AS IDENTITY")
            elif col.get("column_default") is not None:
                parts.append(f"DEFAULT {col['column_default']}")
            if col.get("is_nullable") == "NO":
                parts.append("NOT NULL")
            lines.append("    " + " ".join(parts))

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in constraints:
            key = (item["constraint_name"], item["constraint_type"])
            grouped.setdefault(key, []).append(item)
        for (name, ctype), items in grouped.items():
            cols = [i["column_name"] for i in items if i.get("column_name")]
            qcols = ", ".join(self._quote_ident(c) for c in cols)
            if ctype == "PRIMARY KEY":
                lines.append(
                    f"    CONSTRAINT {self._quote_ident(name)} PRIMARY KEY ({qcols})"
                )
            elif ctype == "UNIQUE":
                lines.append(
                    f"    CONSTRAINT {self._quote_ident(name)} UNIQUE ({qcols})"
                )
            elif ctype == "FOREIGN KEY" and items:
                first = items[0]
                target_schema = first.get("foreign_table_schema")
                target_table = first.get("foreign_table_name")
                target_columns = [
                    i.get("foreign_column_name") for i in items if i.get("foreign_column_name")
                ]
                qtcols = ", ".join(self._quote_ident(c) for c in target_columns)
                if target_schema and target_table:
                    lines.append(
                        "    CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {}.{} ({})".format(
                            self._quote_ident(name),
                            qcols,
                            self._quote_ident(target_schema),
                            self._quote_ident(target_table),
                            qtcols,
                        )
                    )

        create = "CREATE TABLE {}.{} (\n{}\n);".format(
            self._quote_ident(schema),
            self._quote_ident(table),
            ",\n".join(lines),
        )
        index_lines = [
            item["indexdef"] + ";"
            for item in indexes
            if not item.get("is_primary")
        ]
        if index_lines:
            create += "\n\n-- Indexes\n" + "\n".join(index_lines)
        return create

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------

    @staticmethod
    def write_csv(path: Path, result: QueryResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=result.columns)
            writer.writeheader()
            for row in result.rows:
                writer.writerow({name: row.get(name) for name in result.columns})

    # ------------------------------------------------------------------
    # Document ingestion specialized helpers
    # ------------------------------------------------------------------

    def has_rag_schema(self) -> bool:
        required = {"documents", "chapters", "sections", "contents"}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
                existing = {row["table_name"] for row in cur.fetchall()}
        return required.issubset(existing)

    def list_documents(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT document_id, COALESCE(title, document_id) AS title
                    FROM documents
                    ORDER BY title, document_id
                    LIMIT %s
                    """,
                    (limit,),
                )
                return list(cur.fetchall())

    def list_specification_documents(self, limit: int = 1000) -> dict[str, list[dict[str, Any]]]:
        """Return version-aware documents plus generic/unversioned documents."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        s.id AS specification_id,
                        s.specification_key,
                        s.canonical_name,
                        sv.id AS version_id,
                        sv.version_label,
                        sv.version_sort_key,
                        sv.document_id,
                        COALESCE(d.file_name, d.title, sv.document_id) AS document_title,
                        CASE WHEN s.current_version_id = sv.id THEN 'CURRENT' ELSE 'HISTORY' END AS version_role
                    FROM public.specifications s
                    JOIN public.specification_versions sv
                      ON sv.specification_id = s.id
                    JOIN public.documents d
                      ON d.document_id = sv.document_id
                    ORDER BY s.canonical_name, sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                versioned = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT
                        d.document_id,
                        COALESCE(d.file_name, d.title, d.document_id) AS document_title,
                        COALESCE(d.project_code, '') AS project_code
                    FROM public.documents d
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.specification_versions sv
                        WHERE sv.document_id = d.document_id
                    )
                    ORDER BY document_title, d.document_id
                    LIMIT %s
                    """,
                    (limit,),
                )
                generic = list(cur.fetchall())
        return {"versioned": versioned, "generic": generic}

    def list_chapters(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        chapter_id,
                        COALESCE(NULLIF(title_jp, ''), NULLIF(title_en, ''), chapter_id) AS title
                    FROM chapters
                    WHERE document_id = %s
                    ORDER BY COALESCE(sort_order, 0), chapter_id
                    """,
                    (document_id,),
                )
                return list(cur.fetchall())

    def list_sections(self, document_id: str, chapter_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        section_id,
                        COALESCE(NULLIF(title_jp, ''), NULLIF(title_en, ''), section_id) AS title,
                        parent_section_id
                    FROM sections
                    WHERE document_id = %s
                      AND chapter_id = %s
                    ORDER BY COALESCE(sort_order, 0), section_id
                    """,
                    (document_id, chapter_id),
                )
                return list(cur.fetchall())

    def list_contents(
        self,
        document_id: str,
        section_id: str,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        document_id,
                        section_id,
                        chunk_index,
                        page_number,
                        token_count,
                        embedding_status,
                        content
                    FROM contents
                    WHERE document_id = %s
                      AND section_id = %s
                    ORDER BY chunk_index, id
                    LIMIT %s
                    """,
                    (document_id, section_id, limit),
                )
                return list(cur.fetchall())

    def embedding_status_summary(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(embedding_status, '<NULL>') AS status,
                        COUNT(*) AS count
                    FROM contents
                    GROUP BY embedding_status
                    ORDER BY status
                    """
                )
                return list(cur.fetchall())

    def embedding_rows(self, status: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        document_id,
                        section_id,
                        chunk_index,
                        page_number,
                        token_count,
                        embedding_status,
                        LEFT(content, 500) AS content_preview
                    FROM contents
                    WHERE embedding_status = %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    (status, limit),
                )
                return list(cur.fetchall())
