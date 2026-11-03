"""Read-only inspection of documents and verified spec/version bindings.

Do not infer 550 from a PDF filename. No production data is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Support direct `python .\scripts\diagnose_spec_registration_v152.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg
from psycopg.rows import dict_row

from app.server.configuration import load_profiles, user_configuration_dir
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect imported files and approved identity linkage")
    parser.add_argument("--scope", choices=("21MM", "24MM", "COMMON"), default="21MM")
    parser.add_argument("--term", default="Bluetooth Audio")
    parser.add_argument("--spec-code", default="550")
    args = parser.parse_args()
    keyword = args.term.strip()
    if len(keyword) < 3 or len(keyword) > 100 or any(c in keyword for c in "\x00\r\n"):
        parser.error("term must be a bounded, single-line search phrase")
    if not args.spec_code.isdigit() or len(args.spec_code) > 8:
        parser.error("spec-code must be numeric")
    profiles, password, _, _ = load_profiles(user_configuration_dir())
    params = profiles[args.scope].dsn_kwargs()
    params.update(password=password, connect_timeout=3)
    try:
        with psycopg.connect(**params, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL statement_timeout='7000ms'")
                # Left joins preserve imported documents even without a version
                # association. Filtering by filename avoids unrelated BUG/QA.
                cur.execute("""
                    SELECT d.document_id, d.file_name, d.version AS imported_version,
                           d.metadata AS imported_metadata,
                           sv.id AS version_id, sv.version_label, sv.branch_name,
                           sp.id AS specification_id, sp.spec_code,
                           sp.project_code AS linked_project
                    FROM public.documents d
                    LEFT JOIN public.specification_versions sv
                      ON sv.document_id=d.document_id
                    LEFT JOIN public.specifications sp
                      ON sp.id=sv.specification_id
                    WHERE d.project_code=%s AND d.file_name ILIKE %s
                    ORDER BY d.file_name, sv.id DESC NULLS LAST
                    LIMIT 60
                """, (args.scope, f"%{keyword}%"))
                rows = [dict(x) for x in cur.fetchall()]
            conn.rollback()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "sqlstate": getattr(exc, "sqlstate", None)}))
        return 2
    counts: dict[str, int] = {}
    for row in rows:
        primary = PrimarySpecificationDocumentPolicy.is_primary(
            file_name=str(row.get("file_name") or ""),
            metadata=dict(row.get("imported_metadata") or {}),
        )
        if not primary:
            status = "SUPPORTING_ONLY"
        elif (row.get("version_id") is not None and row.get("branch_name") == "MAIN"
              and str(row.get("spec_code") or "") == args.spec_code
              and row.get("linked_project") == args.scope):
            status = "VERIFIED_SPEC_BODY_VERSION_LINK"
        elif row.get("version_id") is not None:
            status = "VERSION_LINK_EXISTS_BUT_SPEC_CODE_OR_BRANCH_UNVERIFIED"
        else:
            status = "IMPORTED_SPEC_BODY_WITHOUT_VERSION_LINK"
        counts[status] = counts.get(status, 0) + 1
        print(json.dumps({"file_name": row["file_name"],
                          "document_id": row["document_id"],
                          "imported_version": row["imported_version"],
                          "specification_id": row["specification_id"],
                          "spec_code": row["spec_code"],
                          "version_id": row["version_id"],
                          "version_label": row["version_label"],
                          "branch_name": row["branch_name"],
                          "status": status},
                         ensure_ascii=True, default=str))
    print(json.dumps({"scope": args.scope, "matching_rows": len(rows), "statuses": counts},
                     ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
