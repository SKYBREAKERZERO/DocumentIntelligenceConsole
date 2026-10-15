# DocumentIntelligenceConsole Enterprise v1.30 CN FULL

v1.30 introduces an internal-only engineering agent retrieval gate: controlled multilingual/fuzzy title discovery, fixed read-only SQL/Qdrant search tools, no-generation-on-empty-evidence, distinct source failure vs no hits, and authenticated read-only per-scope corpus health. The Open WebUI operator must select `document-intelligence-rag` and disable direct model/web-search bypass; these backend code changes do not enforce the external UI's persistent settings. The full deployment checklist is in `docs/ENGINEERING_INTERNAL_AGENT_v1.30_CN.md`. The legacy FastAPI OpenAI-compatible API version stays at v1.20. No live production corpus is changed or certified.

v1.29 fixes the UTF-16 dependency manifest, collection-time database/PDF side effects and introduces explicit opt-in, redacted Windows GUI import / live PostgreSQL read-only transaction probes. See `docs/ENGINEERING_STAGE1_v1.29_WINDOWS_SQL_CN.md`. These probes never constitute production sign-off.

Stage 1 v1.28 introduces `scripts/stage1_release_readiness.py`: static package/dependency preflight and a fail-closed local source hash and identity check spanning native PDF audit, read-only SQL trace and Gold score reports. It never asserts production acceptance or authenticates SQL, reviewer identity or a Windows GUI session. Historical loader smoke tests are now self-contained. See `docs/ENGINEERING_STAGE1_v1.28_RELEASE_ACCEPTANCE_CN.md`.

Stage 1 v1.27 adds opt-in structural Gold review for the PDF/native/SQL source chain, exported-section membership proxy and the **exhaustive pair universe among detected adjacent-page PDF tables**. Independent manual labels are required before Precision/Recall/F1 is defined; legacy Stage1 Gold v3 still evaluates requirement/scope spans. A passing metric gate does **not** certify semantic equivalence, DCM state transitions or production acceptance. See `docs/ENGINEERING_STAGE1_v1.27_STRUCTURAL_GOLD_CN.md`.


**Stage 1 PDF → Pipeline JSON → PostgreSQL Content → Requirement Unit crosswalk.** The new opt-in, read-only `scripts/inspect_stage1_native_sql_chain.py` recomputes native PDF page/text evidence and checks the PostgreSQL project, specification version, source binary SHA-256, backfill receipt, content snapshot, and requirement offsets within a REPEATABLE READ READ ONLY transaction. Missing/ambiguous PDF matches, invalid exported sections, SQL duplicates, unit-offset drift, or truncated review queues are explicitly blocked. The output is redacted and contains only hashes, positions, statuses and SQL numeric identifiers.

This is **literal source corroboration, not semantic certification**. Neither DCM state changes, bilingual requirements, condition/action scope, nor cross-page table headers are automatically approved. The older v1.24 and v1.25 tools remain intact. See [`docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md`](docs/ENGINEERING_STAGE1_v1.26_NATIVE_SQL_CHAIN_CN.md) for PowerShell usage.

**Verification limits:** 16 new synthetic regressions (generated PDFs and fake database cursors); the older `input/test.pdf` collection blocker was removed; six legacy modules may still require `psycopg` / `ttkbootstrap` before collection on a clean machine. There is no production PostgreSQL access, customer-native DCM PDF acceptance, Windows GUI validation or freshly compiled EXE. This is a full source ZIP.

---

## Runtime architecture

The legacy application still contains its Windows GUI, local Backend, Ollama and Open WebUI integration, PostgreSQL authority and Qdrant derived indexes. Preserve your existing secrets, `%LOCALAPPDATA%` configurations, databases and Docker volumes during upgrades; do not run two releases on the same backend port.
