"""Durable, bounded ingestion receipts; never store credentials or source text.

Keep SQL commits when the derived vector stage fails. Retrying vectors is an
explicit operator action, not an automatic replay of document conversion.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IngestionReceipt:
    def __init__(self, directory: Path, *, scope: str, files, database: str, collection: str):
        if scope not in {"21MM", "24MM", "COMMON", "QABUG"}:
            raise ValueError("Explicit supported scope required")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.id = uuid.uuid4().hex
        self.path = self.directory / (self.id + ".json")
        self.data = {"schema": "dic.ingestion-receipt.v1", "id": self.id,
                     "scope": scope, "database": database, "collection": collection,
                     "created_at": utc_now(), "updated_at": utc_now(), "status": "RUNNING",
                     "files": [{"name": Path(p).name, "status": "PENDING"} for p in files],
                     "stages": {"CONVERT_JSON_SQL": "RUNNING", "VECTOR_SYNC": "PENDING", "QUALITY": "PENDING"},
                     "retry": "NONE", "error_type": "", "quality": "NOT_CHECKED"}
        self.save()

    def save(self):
        self.data["updated_at"] = utc_now()
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)

    def file_done(self, index: int, *, success: bool):
        self.data["files"][index]["status"] = "COMPLETED" if success else "FAILED"
        self.save()

    def finish_vectors(self, *, imported: int, failed: int, cancelled: bool,
                       service, validator, cancel_check, progress) -> tuple[str, int]:
        """Return pipeline status and additional warning count, always fail closed."""
        stages = self.data["stages"]
        if cancelled or cancel_check():
            stages["CONVERT_JSON_SQL"] = "CANCELLED"
            stages["VECTOR_SYNC"] = stages["QUALITY"] = "SKIPPED"
            self.data.update(status="CANCELLED", retry="REVIEW_INPUTS")
            self.save()
            return "CANCELLED", 0
        if failed or imported != len(self.data["files"]):
            stages["CONVERT_JSON_SQL"] = "PARTIAL"
            stages["VECTOR_SYNC"] = stages["QUALITY"] = "SKIPPED"
            self.data.update(status="PARTIAL", retry="REVIEW_FAILED_FILES_THEN_SYNC")
            self.save()
            return "PARTIAL", 1
        stages["CONVERT_JSON_SQL"] = "COMPLETED"
        stages["VECTOR_SYNC"] = "RUNNING"
        self.save()
        try:
            result = service.sync(full_rebuild=False, progress=progress, cancel_check=cancel_check)
            if str(result.status) != "READY" or int(result.failed_count):
                self.data.update(status="PARTIAL", retry="VECTOR_SYNC_ONLY")
                stages["VECTOR_SYNC"] = "PARTIAL"
                stages["QUALITY"] = "SKIPPED"
                self.save()
                return "PARTIAL", 1
            stages["VECTOR_SYNC"] = "COMPLETED"
            if cancel_check():
                self.data.update(status="CANCELLED", retry="QUALITY_CHECK_ONLY")
                stages["QUALITY"] = "SKIPPED"
                self.save()
                return "CANCELLED", 0
            stages["QUALITY"] = "RUNNING"
            self.save()
            quality = validator.validate()
            status = str(quality.status)
            self.data["quality"] = status
            stages["QUALITY"] = "COMPLETED" if status == "PASS" else "NEEDS_REVIEW"
            self.data.update(status="COMPLETED" if status == "PASS" else "PARTIAL",
                             retry="NONE" if status == "PASS" else "QUALITY_REVIEW")
            self.save()
            return self.data["status"], int(status != "PASS")
        except Exception as exc:
            was_cancelled = cancel_check() or type(exc).__name__ == "QdrantSyncCancelled"
            for stage in ("VECTOR_SYNC", "QUALITY"):
                if stages[stage] == "RUNNING":
                    stages[stage] = "CANCELLED" if was_cancelled else "FAILED"
            self.data.update(status="CANCELLED" if was_cancelled else "PARTIAL",
                             retry="VECTOR_SYNC_ONLY" if stages["VECTOR_SYNC"] != "COMPLETED" else "QUALITY_CHECK_ONLY",
                             error_type=type(exc).__name__)
            self.save()
            return self.data["status"], int(not was_cancelled)


def recent_receipts(directory: Path, limit: int = 20):
    rows = []
    paths = sorted(Path(directory).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:max(1, min(limit, 100))]:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict) or row.get("schema") != "dic.ingestion-receipt.v1":
                continue
            if not isinstance(row.get("stages"), dict) or not all(isinstance(row.get(k), str) for k in ("scope", "status", "retry")):
                continue
            # A durable RUNNING receipt cannot prove its owning process is alive.
            row["display_status"] = "UNCONFIRMED_RUNNING" if row["status"] == "RUNNING" else row["status"]
            rows.append(row)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return rows
