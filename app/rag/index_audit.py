"""Disk-backed, read-only-source index reconciliation; never repairs or deletes."""
from pathlib import Path
import sqlite3
from typing import Iterable, Any


def reconcile_index(path: Path, expected: Iterable[dict[str, Any]], actual: Iterable[dict[str, Any]]) -> dict:
    """Compare exactly the sync-eligible point IDs, source IDs and body hashes.

    Inputs must come from one SQL snapshot and a fully scrolled collection.
    Index metadata/model fingerprints require a separate sync-plan check.
    Never overwrite an existing audit, and never retain enterprise body text.
    """
    path=Path(path)
    if path.exists():raise ValueError("Audit destination already exists")
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    duplicate=invalid=0
    try:
        db.executescript("""CREATE TABLE expected(point_id TEXT PRIMARY KEY, content_id TEXT, hash TEXT);
            CREATE TABLE actual(point_id TEXT PRIMARY KEY, content_id TEXT, hash TEXT);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);""")
        for table,rows in (("expected",expected),("actual",actual)):
            with db:
                for row in rows:
                    pid=row.get("point_id")
                    cid=row.get("content_id")
                    if pid is None or cid is None:
                        if table=="expected":raise ValueError("SQL eligibility contains invalid source identity")
                        invalid+=1
                    try:
                        db.execute(f"INSERT INTO {table} VALUES(?,?,?)",(str(pid),str(cid),str(row.get("content_hash") or "")))
                    except sqlite3.IntegrityError:
                        if table=="expected":raise ValueError("Duplicate SQL point identity") from None
                        duplicate+=1
        def count(sql):return db.execute(sql).fetchone()[0]
        report={
            "eligible_sql_points":count("SELECT count(*) FROM expected"),
            "actual_unique_points":count("SELECT count(*) FROM actual"),
            "missing_points":count("SELECT count(*) FROM expected e LEFT JOIN actual a USING(point_id) WHERE a.point_id IS NULL"),
            "extra_points":count("SELECT count(*) FROM actual a LEFT JOIN expected e USING(point_id) WHERE e.point_id IS NULL"),
            "source_id_mismatch":count("SELECT count(*) FROM expected e JOIN actual a USING(point_id) WHERE e.content_id<>a.content_id"),
            "body_hash_mismatch":count("SELECT count(*) FROM expected e JOIN actual a USING(point_id) WHERE e.hash<>a.hash AND e.hash<>'' AND a.hash<>''"),
            "unverifiable_body_hash":count("SELECT count(*) FROM expected e JOIN actual a USING(point_id) WHERE e.hash='' OR a.hash=''"),
            "duplicate_scrolled_ids":duplicate,"invalid_point_payloads":invalid,
            "read_only_sources":True,"automatic_repair":False,
            "metadata_model_fingerprint_verified":False,
        }
        report["id_and_body_coverage_complete"]=not any(report[k] for k in (
            "missing_points","extra_points","source_id_mismatch","body_hash_mismatch",
            "unverifiable_body_hash","duplicate_scrolled_ids","invalid_point_payloads"))
        db.execute("INSERT INTO metadata VALUES('complete','true')")
        db.commit()
        return report
    finally:db.close()
