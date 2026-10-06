from __future__ import annotations

import argparse
import getpass
import json
import os

from app.storage.configuration_schema_manager import ConfigurationSchemaManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the enterprise app_config PostgreSQL schema.")
    parser.add_argument("--host", default=os.getenv("POSTGRES_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("POSTGRES_PORT", "5432")))
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", default=os.getenv("POSTGRES_USER", "postgres"))
    parser.add_argument("--project", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    password = os.getenv("POSTGRES_PASSWORD") or getpass.getpass("PostgreSQL password: ")
    manager = ConfigurationSchemaManager(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        project_code=args.project or None,
    )
    report = manager.inspect_schema()
    payload = {
        "ready": report.ready,
        "version": report.version,
        "required_tables": list(report.required_tables),
        "available_tables": list(report.available_tables),
        "missing_tables": list(report.missing_tables),
        "missing_columns": {k: list(v) for k, v in report.missing_columns.items()},
        "warnings": list(report.warnings),
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report.status_text())
        if report.missing_tables:
            print("Missing tables:", ", ".join(report.missing_tables))
        for table_name, columns in report.missing_columns.items():
            print(f"{table_name}: missing {', '.join(columns)}")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
