# CHANGELOG

## v1.03

- Reclassified SQL/Qdrant freshness backlog from runtime ERROR to WARNING while preserving hard errors for vector/data-contract violations.
- Treats a missing Qdrant collection as a valid empty scope when authoritative SQL current count is zero.
- Added explicit QA/BUG structured chunk fan-out diagnostics so record count and chunk count are not mistaken for duplication.
- Health diagnostics now recommend controlled incremental SQL -> Qdrant sync for missing/stale/orphan derived points instead of implying a forced full rebuild.
- Added a testable `app.ai.health_policy` severity policy and v1.03 regression tests.

## v1.02

- Simplified the AI workspace UI: the main heading is now `AI工作台`, the initial AUTO marketing paragraph was removed, and the first chat surface opens blank.
- Hid `Embedding Monitor` and `Qdrant Browser` from the left navigation while retaining internal pages/tools.
- Reset the application release line to v1.02.
- Added `PrimarySpecificationDocumentPolicy` as the single authority for specification body vs supporting artefacts.
- Specification Lookup now resolves the authoritative body before Cover Sheet / Check Sheet / Test Sheet.
- Current RAG read models and Qdrant sync exclude supporting/non-primary artefacts, including legacy filenames without modern metadata.
- Runtime Qdrant evidence rejects stale Cover Sheet points even before the collection is rebuilt.
- Engineering Relation retrieval uses the preferred body version and the latest READY extraction for that body instead of trusting a polluted legacy current pointer.
- Manual Engineering Relations use the same preferred-body selection.
- Version History, Version Compare, Change Timeline and AI version retrieval are body-only.
- Diff engine rejects supporting artefacts as comparison endpoints.
- One-click schema upgrade non-destructively backfills legacy supporting-document metadata.
- One-click schema upgrade repairs legacy mutable `current_version_id` pointers that target supporting artefacts.
- Cover-only legacy specification masters are excluded from the governed specification catalog.
- Legacy version backfill and version-chain reconstruction operate on primary specification bodies only.
- Qdrant retrieval over-fetches before legacy filename arbitration so a Cover Sheet cannot consume all Top-K candidates.
- Build preflight was repaired and now uses the v1.02 release baseline.
- Existing immutable audit/history rows are retained; no destructive database cleanup is performed.
