# Document Intelligence Console v1.03

`v1.03` continues the consolidated 1.x application release line; future releases increment from this baseline.

## v1.03 RAG health governance

- Index freshness gaps are reported as `WARNING · Index sync required` when Ollama/PostgreSQL/Qdrant dependencies are healthy.
- A missing Qdrant collection for an authoritative SQL scope with zero current chunks is treated as a valid empty scope (`SKIPPED`).
- Vector dimension, distance, point identity and payload contract violations remain hard `ERROR` conditions.
- QA/BUG health diagnostics expose the structured field-level record-to-chunk fan-out and distinguish it from duplicate records.
- SQL/Qdrant backlog is repaired through incremental sync rather than forcing a full rebuild.

## Primary specification-body arbitration

A logical specification can own multiple physical files: the main specification body, Cover Sheet, Check Sheet, Test Sheet, translations and other supporting artefacts. v1.02 defines one authoritative rule across the product:

- `PRIMARY_CONTENT`: specification body; authoritative for RAG, engineering relations, version history and version comparison.
- `METADATA_SUPPORT`: Cover Sheet and similar artefacts; retained for metadata/audit only and never allowed to replace the body.

The same `PrimarySpecificationDocumentPolicy` is enforced by Specification Lookup, Current SQL RAG views, Qdrant sync/runtime filtering, exact SQL retrieval, Engineering Relations and version comparison.

Legacy databases are supported. The database one-click upgrade marks obvious supporting files as `SUPPORTING / METADATA_SUPPORT / EXCLUDE` without deleting immutable history. Read models also bypass a legacy `current_version_id` that points to a Cover Sheet and select the preferred primary body instead.

## Version comparison

Only `PRIMARY_CONTENT -> PRIMARY_CONTENT` endpoints are legal. Supporting, metadata-only, translation and issue-register artefacts are excluded from business-version selectors and rejected by the diff engine.

## Upgrade from an older package

Run the enterprise database one-click upgrade once for each active 21MM/24MM/Common database, then run one SQL -> Qdrant incremental sync for the affected scope. This refreshes read views and removes legacy supporting-document points while restoring the preferred body to the Current index.

The application version is v1.03. The internal PostgreSQL `RAG Schema v6` identifier is retained as a database compatibility contract and is not the application release version.
