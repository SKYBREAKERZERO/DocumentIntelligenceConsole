# DocumentIntelligenceConsole Enterprise v1.52 CN FULL


**v1.52 PostgreSQL factual lookup remediation:** fixes the `IndeterminateDatatype` failure, preserves independently verified imported-file evidence, and provides a read-only 550/version-linkage diagnostic. An imported PDF is not proof of a trusted 550 MAIN SPEC_BODY version; see `docs/ENGINEERING_v1.52_BLUETOOTH_SQL_FIX_CN.md`.
## v1.52 — deterministic SQL specification discovery and CPU-friendly routes

- New SQL-only factual routes: document/version existence, registered specification catalog, verified-body chapter index and adjacent saved version diff. No Ollama needed.
- Body-only; no cover sheet substitution; fixed duplicated PostgreSQL health-check timeout keyword. Live acceptance remains pending.
- Validation: `docs/ENGINEERING_v1.52_SPEC_FACTUAL_QA_CN.md`.

## v1.50 — historical specifications, QA/BUG and engineering relation Skills

- Deterministic read-only cross-domain planning adds required issue, SQL, version and relation tools even when the local model omits them; bounded to the existing allow-list and authorized scopes.
- Evidence governance preserves available issue/spec/history/relation facets, with conservative treatment of missing facets.
- Bounded PostgreSQL-authored BUG symptom queries can initiate a second SQL specification lookup; this does not establish version applicability or issue conformance.
- Release gates protect against unreviewed conformance and engineering dependency claims, and a narrow ON/OFF numeric condition mismatch. Large BUG exports receive whole-field, explicitly incomplete previews rather than unsafe text clipping.
- **Development only:** no real PostgreSQL/Qdrant, Windows runtime, customer Golden semantic accuracy or production signoff. See `docs/ENGINEERING_v1.50_CROSS_DOMAIN_SKILL_QUALITY_CN.md`.

## v1.49 — governed engineering Agent Skills (development-only)

Seven typed Skills are installed under `config/skills/` with fixed-function offline Python execution and examples. The evidence-verification Skill is wired into the enterprise RAG release gate. The six other Skills are local/manual, NOT automatically routed by Open WebUI or LangGraph. Offline JSON inputs are not database-certified evidence; no SQL/Qdrant writes and no production signoff. See `docs/ENGINEERING_v1.49_SKILLS_CN.md`.

## v1.48 — enterprise answer integrity hardening (production NOT approved)

- Every Dense or QA/BUG vector hit must be dereferenced against PostgreSQL and match the authoritative document identity, version and actual UTF-8 content SHA-256. SQL exact and Sparse text integrity checks are strengthened; incomplete legacy vector payloads are rejected rather than trusted.
- Enterprise LLM replies are buffered until citation IDs are checked against the *actual prompt evidence*. Invalid/missing citations or uncited factual paragraphs produce a fixed insufficient-evidence response, with no speculative early SSE tokens. This only verifies structural references, **not semantic entailment or engineering correctness**.
- Specification evidence labels include PDF page (or `PAGE_UNKNOWN`), content ID, file and content digest. Oversized requirements are omitted whole, and incomplete coverage is disclosed instead of silently truncating exceptions or table footnotes.
- Local API now enforces bounded input size/model allow-list and sanitized server errors. See `docs/ENGINEERING_v1.48_ANSWER_QUALITY_REMEDIATION_CN.md` for rollout and remaining enterprise acceptance blockers.

## v1.46 — optional CPU CrossEncoder reranker and Golden latency benchmark

Adds an **opt-in**, local-only CPU CrossEncoder behind the SQL-verified SPEC_BODY RRF path, using a bounded, killable spawn process for hard timeouts. Missing local model, invalid output, over-budget inference, or concurrency overload falls back to the unchanged RRF order. QA/BUG, relations, temporal evidence and unverified dense-only payloads are excluded. A separate offline Golden replay can measure Recall@K/MRR@K and wall-clock p50/p95, including cold model startup; no actual customer model or live database performance acceptance has been performed. See `docs/ENGINEERING_v1.46_LOCAL_RERANKER_CPU_BENCHMARK_CN.md`.


## v1.45 — Stage 4 native BGE-M3 Sparse sidecar + three-way RRF

Adds optional FlagEmbedding lexical token-ID weighting and a separate Qdrant sparse-only sidecar collection without migrating the legacy unnamed Dense collection. Index construction is dry-run by default; `--write` explicitly modifies only the sidecar. Online Sparse hits are always hydrated from authoritative project PostgreSQL and checked against document identity, version, SPEC code and content hash. With `DIC_STAGE4_SPARSE_ENABLED=1`, SQL + Dense + Sparse are RRF-fused when available; legacy two-way retrieval remains the default. Offline Golden evaluation now reports Sparse and three-way RRF separately. No real model/PG17/Qdrant/Windows/customer PDF validation or local CrossEncoder reranker has been completed. See `docs/ENGINEERING_v1.45_BGE_M3_SPARSE_SIDECAR_CN.md`.



## v1.44 — Stage 4 phase-one SQL + BGE-M3 Dense RRF baseline

Engineering SPEC_BODY SQL exact and Qdrant Dense chunk candidates now use scoped, version-specific, bounded Reciprocal Rank Fusion, with SQL body authority and source-text mismatch rejection. Relations, temporal audit, QA/BUG, and unreviewed candidates remain isolated. `scripts/evaluate_stage4_rrf.py` reports offline human-labeled Recall@K/Precision@K/MRR@K without DB or index writes. **Sparse Qdrant retrieval and a local neural Reranker are NOT implemented yet**; no live PostgreSQL/Qdrant/Windows/PDF acceptance or measured production thresholds. Read `docs/ENGINEERING_v1.44_STAGE4_RRF_BASELINE_CN.md`.

## v1.43 — explicit historical relation audit in Agent + PG17 deployment smoke checks

Explicitly labeled relation ID and later target version ID now invoke the v1.42 verifier through the Agent's **single-project, single-snapshot READ ONLY** route. The original question, not model rewrites, supplies coordinates. Missing/ambiguous project or database identity fails closed. No general semantic retrieval or adaptive recovery is mixed into the audit. Audited target text continuity **never** implies engineering relation validity: `relation_validity=UNDETERMINED`, and human revalidation is always required.

Added `scripts/verify_stage3_postgres_runtime.py` for operator-triggered PostgreSQL 17 schema, project identity and effective reader privilege checks. An offline PASS is not live PostgreSQL acceptance. No migrations, auto-approved relations, database writes, live Windows/508→550 PDF validation or production certification. See `docs/ENGINEERING_v1.43_AGENT_TEMPORAL_RUNTIME_CN.md`.

## v1.42 — read-only historical relation to new target requirement audit

Added `relation_temporal_audit.py` and `scripts/audit_stage3_relation_timeline.py`: given an existing CONFIRMED relation ID and an explicit later target version ID, a bounded PostgreSQL READ ONLY snapshot checks independent PRIMARY/SPEC_BODY source spans and hashes, approved version order, receipts, unique READY comparison and an existing deterministic/approved v7 requirement mapping. The result distinguishes target text continuity, movement, modification, removal or insufficient evidence **without** claiming that an engineering relation remains valid, becomes invalid or causes impact. `relation_validity` stays UNDETERMINED; source-version release alignment remains NOT_ESTABLISHED. No migrations, automatic approval, GUI/Agent integration or live PostgreSQL/Windows/508-to-550 production acceptance. See `docs/ENGINEERING_v1.42_TEMPORAL_RELATION_AUDIT_CN.md`.

## v1.41 — SPEC_BODY version repair and Stage 2 index integrity audit (read-only)

`STRUCTURAL_REFERENCE` and `STRUCTURAL_LAYOUT` now select the authoritative MAIN SPEC_BODY even if the old `current_version_id` points to a Coversheet; they do not silently reuse stale indexed versions. Adjacency uses the actual next unit in the *unfiltered* PostgreSQL index and cannot skip an intervening unit hidden by a chapter filter. Rejected endpoints are excluded from source and target citations. New opt-in `scripts/audit_stage2_relation_index.py` checks a complete v7 receipt, full source-block fingerprint (including zero-unit chunks), every indexed unit's Unicode span/SHA-256 and total count in a bounded `REPEATABLE READ READ ONLY` transaction. Failures and truncation are BLOCKED. A PASS is only integrity of the selected database snapshot, not semantic coverage, review authority or production certification. Prior relation validity remains UNDETERMINED. No migration or writes. See `docs/ENGINEERING_v1.41_RELATION_INDEX_INTEGRITY_CN.md`. No live DB, Windows GUI, real PDFs or large-scale p95 acceptance.

## v1.40 — read-only layout relations and version-change revalidation

Adds `STRUCTURAL_LAYOUT` for independently hash-verified same-section, direct parent/child section and consecutive indexed-unit facts within an authoritative SPEC_BODY document. When a question omits a specification number, previously retrieved `PRIMARY_CONTENT` specification identities can be bounded lookup candidates (never proof). A verified explicit reference may be joined to an existing READY historical chapter diff only if scope, target specification, current target version, body document and section align. `REFERENCE_CHANGE_REVALIDATION` means **human revalidation needed, not stale, dependent, impacted or noncompliant**. No schema migration or automatic approval. See `docs/ENGINEERING_v1.40_LAYOUT_DISCOVERY_AND_CHANGE_REVALIDATION_CN.md`. No live PostgreSQL, Windows GUI, real-PDF or performance acceptance was performed.

v1.39 adds separate read-only paths for **rule-verified explicit SPEC/section references** (`STRUCTURAL_REFERENCE`) and **hash-checked persisted chapter-level revision differences** (`CHAPTER_VERSION_DIFF`). The former requires two valid PRIMARY/SPEC_BODY text spans and an exact source reference anchor; it is **not** a human-approved engineering dependency. The latter filters existing READY body-to-body change records by explicit section/keyword/version pair and independently checks SHA-256 on stored original text. Neither feature writes or automatically approves anything, and neither depends on v1.33 review functions. Real PostgreSQL 17, Windows GUI, source PDF and performance acceptance are NOT VERIFIED. See `docs/ENGINEERING_v1.39_STRUCTURAL_REFERENCES_AND_CHAPTER_DIFF_CN.md`.

v1.38 adds an opt-in *QABUG-only* schema for manually approved exact software-to-historical-specification version mappings, offline BUG observation-to-SPEC_BODY requirement candidate indexing, and independent display-only candidate retrieval in the GUI and non-streaming local API. The lexical candidate is **not evidence of conformance**, remains UNREVIEWED and is never included in Ollama prompt context. It is checked against both PostgreSQL source databases (historical version, document role, exact text offsets and SHA-256) before display. The batch defaults to read-only dry-run; migrations and updates require explicit DBA/operator action. No real PostgreSQL/Windows/508-to-550 acceptance or LangGraph/Sparse/RRF/Reranker implementation is claimed. See `docs/ENGINEERING_v1.38_BUG_SPEC_OFFLINE_CANDIDATES_CN.md`.

v1.37 introduces an opt-in PostgreSQL 17 trigger upgrade for server-enforced, conservative source SPEC/section proof (not dependency proof), target requirement uniqueness, pinned function-body preflight, and a read-only audit-based reconciliation tool for uncertain COMMIT results. The database migration is NOT run by extracting this ZIP; live PostgreSQL/Windows/customer PDF acceptance is outstanding. See `docs/ENGINEERING_STAGE2_v1.37_DB_SEMANTIC_GATE_AND_RECONCILIATION_CN.md`.

Previously v1.36 adds deterministic, source-exact same-clause SPEC/section anchor validation for the Stage 2 508→550-style relation pipeline and audited RAG retrieval; references are not equated to proven functional dependencies. Ambiguous multi-requirement target sections cannot be arbitrarily selected. The reviewer workbench and read-only graph expose literal anchor spans. A lost PostgreSQL response at COMMIT is explicitly marked outcome-unknown, not "rolled back". No live DB DDL or data writes; v1.33 authenticated server-side gate remains unchanged. Production PostgreSQL/Windows/real-PDF acceptance is pending. See `docs/ENGINEERING_STAGE2_v1.36_REFERENCE_ANCHOR_CN.md`.

v1.35 adds a read-only PostgreSQL database-wide CONNECT-capable LOGIN role audit to the v1.34 preflight, including effective function execution, reviewer enrollment, SET ROLE / owner membership, column-level UPDATE, TRIGGER / TRUNCATE and schema CREATE, with a bounded complete-scan requirement. Both guard functions and the function owner are checked for privilege/configuration drift. GUI review and audit preflight have transaction-local lock/statement timeouts. No database DDL, GRANT, source data or human review state is changed. Offline tests are not real PostgreSQL/Windows acceptance. See `docs/ENGINEERING_STAGE2_v1.35_GLOBAL_ROLE_AUDIT_CN.md`.

v1.34 permanently disables the obsolete client-side reviewer DML API. It adds a separate Stage 2 launch control inside the main Settings GUI and an opt-in, read-only, fail-closed PostgreSQL role/grant/function/trigger preflight (`scripts/preflight_stage2_db.py`). Any failed security check blocks readiness (exit 2); no database schema, live grants or reviewer decisions are changed. See `docs/ENGINEERING_STAGE2_v1.34_PERMISSION_PREFLIGHT_CN.md`.

v1.33 adds a separate local Tk Stage 2 human-review GUI and a DBA-installed PostgreSQL SECURITY DEFINER review authority with session_user allowlist, source/target SHA-256 preconditions, row-locked current-version checks and atomic audit events. No automatic approval, schema installation or direct DML by review clients. Explicit unknown document roles are rejected as body evidence. See `docs/ENGINEERING_STAGE2_v1.33_CONTROLLED_REVIEW_CN.md`. Live PostgreSQL 17, Windows GUI, authorization and actual 508→550 evidence acceptance remain pending.

v1.32 added a read-only, offline Stage 2 content relation graph review workspace. Exports use a REPEATABLE READ READ ONLY PostgreSQL snapshot and source-evidence verification, produce static SVG/HTML (no JavaScript or external resources) and JSON plus a SHA-256 manifest, and quarantine invalid candidates without leaking unverified source text. These artifacts are sensitive, historical, and not signed attestations. See `docs/ENGINEERING_STAGE2_v1.32_CONTENT_GRAPH_EXPORT_CN.md`. No schema migration, live DB acceptance, or auto-approval. The v1.30 GitHub commit remains the common source base; v2.4.22 is a separate unprovided lineage.

v1.31 (based on the main-branch v1.30 source) adds human review for Stage 2 content relation proposals, verifies both verbatim source spans and hashes against PostgreSQL contents, constrains review to current same-project specification-body documents, and requires an audit-matching confirmation for any content-level edge entering AI context. It adds an operator CLI queue/preview/commit path. No schema changes, implicit database writes, or real Windows/PostgreSQL production acceptance. See `docs/ENGINEERING_STAGE2_v1.31_CONTENT_RELATION_REVIEW_CN.md`. This is **not** built from any separate v2.4.22 local codebase.

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
