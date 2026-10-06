from __future__ import annotations

import argparse
import getpass
import json
import os

import psycopg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit current Toyota specification-reference precision and graph candidates."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--project", default=None)
    return parser.parse_args()


def _meta(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env)
    if not password:
        password = getpass.getpass(f"PostgreSQL password for {args.user}: ")

    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=password,
        connect_timeout=10,
        application_name="dic-toyota-reference-precision-audit",
    ) as conn:
        with conn.cursor() as cur:
            where = "cat.is_current_extraction IS TRUE AND cat.relation_type = 'REFERENCES'"
            params: list[object] = []
            if args.project:
                where += " AND src.project_code = %s"
                params.append(args.project)
            cur.execute(
                f"""
                SELECT
                    cat.relation_id,
                    src.spec_code,
                    cat.source_specification_key,
                    cat.source_specification_name,
                    tgt.spec_code,
                    cat.target_specification_key,
                    cat.target_specification_name,
                    cat.target_external_key,
                    cat.status,
                    cat.confidence,
                    cat.evidence_count,
                    cat.extraction_method,
                    cat.metadata
                FROM public.engineering_relation_catalog cat
                JOIN public.specifications src ON src.id = cat.source_specification_id
                LEFT JOIN public.specifications tgt ON tgt.id = cat.target_specification_id
                WHERE {where}
                ORDER BY COALESCE(src.spec_code, cat.source_specification_key), cat.relation_id
                """,
                tuple(params),
            )
            rows = list(cur.fetchall())

    if not rows:
        print("No current specification-reference relations.")
        return 0

    primary = 0
    title_only = 0
    suspicious = 0
    for row in rows:
        (
            relation_id,
            source_code,
            source_key,
            source_name,
            target_code,
            target_key,
            target_name,
            external,
            status,
            confidence,
            evidence_count,
            method,
            metadata,
        ) = row
        meta = _meta(metadata)
        identity = str(meta.get("target_identity_kind") or "").upper()
        semantic = str(meta.get("toyota_reference_kind") or "REFERENCES_SPEC").upper()
        external_text = str(external or "")
        if identity == "TITLE_ONLY" or external_text.startswith("SPEC_TITLE:"):
            category = "TITLE_ONLY"
            title_only += 1
        else:
            category = "PRIMARY"
            primary += 1
        if external_text.startswith("SPEC:"):
            token = external_text[5:]
            if any(word in token.upper() for word in ("FUNCTION", "SPECIFICATION", "BLUETOOTH", "REPORTING", "SYSTEM/")):
                category = "SUSPICIOUS"
                suspicious += 1
        target = target_code or target_key or target_name or external_text or "<none>"
        source = source_code or source_key or source_name
        print(
            f"{category:<10} {status:<10} {source} -> {target} | "
            f"{semantic} | {method} | conf={float(confidence):.3f} | ev={evidence_count} | id={relation_id}"
        )

    print()
    print(f"Summary: primary={primary} title_only={title_only} suspicious={suspicious} total={len(rows)}")
    if suspicious:
        print("WARNING: suspicious primary graph identities remain; re-run the relation backfill and inspect evidence.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
