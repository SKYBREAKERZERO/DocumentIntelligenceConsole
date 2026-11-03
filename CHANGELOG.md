## v1.54 - Conversation intelligence and answerable-evidence phase 1 (2026-11-02)

- Added a user-authored conversation state for the active QA/BUG ticket, specification, project, version, and source-to-target relation.
- Added question frames that distinguish explaining a recorded ticket conclusion from performing a new current conformance judgment.
- Added an answer-usability gate that removes UUID-dominated, header-only, and coordinate-only semantic chunks before model prompting.
- Added explicit answer contracts so the model separates ticket facts, independently verified specification facts, and semantic candidates.
- Expanded natural relation follow-ups such as "508和542有关" / "把那部分展开" without adding one-off document rules.
- Added regression coverage for ticket/spec identity isolation, current-vs-recorded judgments, and noisy Excel evidence.

## v1.54 - Multi-turn specification identity and CPU-only chapter evidence (2026-10-27)

- Queries with introductory verbs and exact versioned PDF name now use PostgreSQL SPEC_LOOKUP (no Ollama).
- Follow-up "按章节总结一下内容" / "总结一下" reuses only explicit PDF identity from earlier **user** messages (no assistant-generation identity inference).
- New SPEC_CHAPTER_CONTENT read-only path supplies bounded PostgreSQL original-text excerpts with page and content IDs; clearly labels incomplete coverage and never promotes Cover Sheet.
- Structured fact requests have their own bounded admission lane, avoiding busy responses caused by long Ollama chats.
- Keeps 550/BT numeric identity unresolved; no PostgreSQL/Qdrant migration is performed.

# Changelog

## v1.53 — Safe body-document read path for unverified 550 identity (2026-10-27)

- Actual 21MM registry audit: 550 Cover Sheet is spec_id=2/version_id=2 and v2.52 PDF is spec_id=3/version_id=3 with NULL spec_code; regional scope remains unresolved.
- PostgreSQL-only BODY candidate query joins MAIN versions without asserting the user's numeric specification identity. Returns `SPEC_IDENTITY_CANDIDATE`, `LIMITED` evidence, the actual linked version ID, and source file/document IDs.
- Candidate PDF chapters/sections can now be read directly by document ID, explicitly labeled as unapproved numeric identity. Cover Sheet is never admitted as technical chapters or another body version.
- Read-only validation tool for Windows checks actual candidate/outline/version status rather than route only; API returns physical document identifiers. No database or Qdrant mutations; production status remains NO-GO.

## v1.52 — PostgreSQL specification lookup repair (2026-10-27)

- Fixed PostgreSQL `IndeterminateDatatype` in the MAIN SPEC_BODY lookup by explicitly typing nullable version parameters as `text` and spec-code arrays as `text[]`.
- Version lookup failure no longer suppresses a separate read-only imported-file check; unresolved linkage is labeled as unverified, not a registered SPEC_BODY version.
- Bare versioned filenames use the SQL-only identity route; no 4B model call required.
- Unbound body files cannot be presented as certified TOC or historical version comparison.
- Added Windows PowerShell 5.1 UTF-8 BOM validation script, real-record regression fixtures, and a read-only PostgreSQL linkage diagnostic. No production DB or Qdrant writes.
- Real 21MM evidence supplied during diagnosis: `Bluetooth Audio function spec v2.52.pdf` exists in `documents`, but the trusted 550 MAIN/version relation is not yet independently demonstrated.


## v1.51 — SQL-only specification factual answer paths (2026-10-27)

- Eliminated Ollama-dependent refusal on document existence, 21MM/24MM catalog, registered source chapters and adjacent verified SPEC_BODY version inventories. Metadata queries now have a deterministic SQL-backed renderer and expose their true limits.
- Exact filename/BT alias discovery, explicit version-label filtering, non-primary exclusions and separate detection of imported but unlinked PDF files.
- Added controlled `/api/v1/retrieval` operations and local PowerShell smoke check. Fixed repeated `connect_timeout` causing false SQL health failures.
- No data migrations/writes, no external calls and no auto-approval. Genuine customer PostgreSQL and PDF/Golden end-to-end verification remains pending (NO-GO).

## v1.50 — safety-gated cross-domain Skill workflow & issue-originated retrieval (2026-10-26)

- Added mandatory bounded read-only tool plans for historical specification, QA/BUG and engineering relation queries; reserve evidence across domains instead of losing relevant contexts to global Top-K ranking.
- Added an optional second-pass lexical spec lookup based only on authoritative PostgreSQL issue symptom fields; never derive evidence from post-fix effect results, never infer issue-to-spec applicability or confirm semantic candidates.
- Added safe whole-field ISSUE projection for local-model context limits and blocked unsupported reviewed dependency/conformance language. Implemented narrow ON/OFF numeric parameter anchoring checks.
- Fixed issue-key number -> spec number false-positive. Added synthetic reproducible workflow profile evaluator, unit/negative tests and deployment restrictions.
- **No new schema migration, index writes, third-party runtime dependencies or permission changes.** Real customer Golden, PostgreSQL 17, Qdrant and Windows GUI validation not done. Production NO-GO.

## v1.49 — seven task-specific engineering Skills and bounded local runner (2026-10-26)

- Added Stage 1/2/3/4/6/7 Skill instructions, fixed catalog, typed read-only offline Python adapters, seven synthetic demo inputs and negative contract tests.
- Integrated `rag-evidence-verification` into the existing enterprise answer publication gate without allowing model-provided evidence IDs.
- No network, dynamic Python/SQL, PostgreSQL/Qdrant updates or automatic approvals. Only the evidence Skill is live; other Skills remain manual offline previews.
- Corrected the Windows fixed version numeric resource from the stale 1.47. Production status remains NO-GO pending real customer Golden and database validation.

## v1.48 — enterprise literal claim anchors, non-exhaustive query gate and quality audit (2026-10-26)

- Added a deterministic answer-release safeguard checking that version literals, engineering numbers and physical units are present in the **same cited admitted evidence**. Supports exact Decimal-based conversion for a bounded set of time/frequency/voltage/current units. Fails closed on unmatched literals; it does NOT prove applicability, negation, causality or semantic entailment.
- Refuse open-ended requests that demand ALL requirements/conditions when only bounded Top-K engineering text search is available. Legacy context helper no longer clips partial requirement text.
- Added an explicit synthetic regression set and CLI answer-gate evaluator. Its result is NOT a customer Golden Dataset or production signoff.
- Retained PostgreSQL-backed SPEC_BODY and reviewed/unreviewed evidence separation. No database migration or index writes. Local tests only; real customer Golden, PG17, Qdrant, Windows and resource limits remain unvalidated. Production acceptance stays NO-GO.

## v1.47 — evidence-backed enterprise answer release gates (2026-10-23)

- Closed locally reproducible Dense payload provenance bypass with mandatory SQL truth hydration, document/revision identity and actual SHA-256 verification. Extended SQL exact and Sparse digest checks; fail closed on missing/stale legacy vector metadata.
- Added post-generation, pre-stream enterprise citation-ID and uncited-paragraph gate. No invalid citations are released as partial SSE chunks. The gate is **syntactic**, not semantic claim entailment, and has not been validated on a real Golden Dataset.
- Added PDF page/content ID/source-hash citation labels; refuse mid-clause truncation of engineering requirements and disclose omitted evidence. Added API request bounds/model allow-list and diagnostic redaction.
- No schema migration or database/index writes. Offline tests only; live Postgres/Qdrant, Windows GUI, CPU performance and customer evidence remain unvalidated. Production acceptance: NO-GO.

## v1.46 — Stage 4 local CPU reranker, bounded inference and latency benchmark (2026-10-23)

- Optional local-directory CrossEncoder CPU ranking of SQL-verified SPEC_BODY RRF candidates only. No change to trusted relation, temporal, QA/BUG or user-visible unreviewed-candidate evidence boundaries. Ranking scores are not engineering confidence.
- Spawn-isolated, single-slot CPU inference with hard deadline, input/batch/thread bounds, local-only loading, and exact RRF fallback on exceptions, timeout or invalid scores. No database migrations/index writes; no Windows frozen EXE or cross-platform hard memory cap certification.
- Offline Golden replay benchmark records per-query measured Recall@K/Precision@K/MRR@K and p50/p95 CPU wall-clock latency (includes model cold start but excludes SQL/Qdrant/LLM runtime). Real customer corpus, on-device CrossEncoder weights, PG17/Qdrant integration and quality thresholds have NOT been validated.

## v1.45 — Stage 4 BGE-M3 native Sparse sidecar and 3-way RRF (2026-10-23)

- Added optional CPU FP32 FlagEmbedding BGE-M3 learned lexical token weights and a separate sparse-only Qdrant sidecar with named sparse vector; the existing unnamed Dense current index remains untouched. The bounded build CLI defaults to read-only preview; --write is an explicit Qdrant-sidecar operation only.
- Online sparse lookup uses SQL-hydrated SPEC_BODY text and independently checks project, point/content ID, document, filename, version, spec code and authoritative content hash. Rejected/stale hits cannot raise ranking. Missing optional components retain the v1.44 two-way path.
- Agent and direct hybrid paths support optional SQL/Dense/Sparse RRF; Golden evaluation distinguishes per-source and two-/three-way Recall@K, Precision@K, MRR@K. RRF remains ranking-only. Offline tests do not establish live Qdrant, PostgreSQL, Windows, CPU latency, original PDFs, or a neural reranker.

## v1.44 — Stage 4 scoped SQL/Dense RRF and offline retrieval baseline (2026-10-23)

- Replaced tool-order concatenation with identity-scoped, bounded Reciprocal Rank Fusion for SQL-exact and Qdrant Dense SPEC_BODY chunks in the Agent hybrid path. Cover sheets, missing source identities, QA/BUG and relationship/version candidates cannot be fused.
- Conflicting SQL vs Dense original source text/content hashes cancel the Dense RRF vote, preserving the SQL copy. RRF score explicitly means *ranking only*, never relationship approval or compliance confidence.
- Added Golden Dataset JSONL read-only evaluator for SQL/Dense/RRF Recall@K, Precision@K and MRR@K with opt-in thresholds and fail-closed invalid-input handling. No SQL migrations, Qdrant index changes, live database certification or Windows testing. BGE-M3 native Sparse and local Reranker remain pending.


## v1.43 — Agent read-only temporal audit & PG17 runtime smoke (2026-10-23)

- Integrated explicit relation ID + new target version ID temporal verification into the Agent, with an isolated original-question-only, project-bound, fail-closed READ ONLY snapshot. Untrusted LLM rewrites never choose identity or issue SQL. Exact temporal audit does not mix vector candidates or trigger broad adaptive recovery.
- Evidence governance and prompts distinguish audited target mapping status from UNDETERMINED engineering relation validity, and incomplete audits stay low-grade diagnostics. No human approval inheritance, stale verdicts, or compliance/impact inference.
- Added standalone opt-in PostgreSQL 17 runtime schema/project/restricted-role/column-write/owner-membership smoke preflight. Synthetic negative tests are not live PG acceptance. No migrations/DB writes/Qdrant modifications; Windows/real 508-to-550 PDF and performance pending.

## v1.42 — read-only temporal relation continuity audit (2026-10-23)

- Introduced explicit historical edge ID → later target SPEC_BODY version read-only audit with verified source/old/new requirement text, offsets and hashes, version identity/order, complete receipt count identity, unique READY version pair and existing deterministic/approved v7 requirement mapping.
- Extractive target statuses are separated from engineering dependency validity, which remains UNDETERMINED in all outcomes. No carrying forward CONFIRMED or emitting automatic invalidation/stale claims; source-version effective-date alignment remains unestablished.
- Added negative tests for Coversheets, fabricated/ambiguous mappings, mismatched version sequences, source drift, incomplete receipts and conflicting status/text. Supplied bounded CLI with strict project/database identity guard and no writes. No live PostgreSQL, Windows, customer PDF or Stage 4 acceptance claimed.

## v1.41 — Body-version selection, full-source index receipt, filtered-adjacency safety (2026-10-23)

- Fixed structural read models to choose the actual MAIN SPEC_BODY version when a historical current pointer references a Coversheet. Do not fallback to a stale indexed version and do not use REJECTED source/target spans for explicit citations.
- Fixed false `ADJACENT_INDEXED_UNITS` under a chapter filter: only a DB-derived next-indexed-unit ID over the unfiltered document/version/content corpus can prove consecutive extracted units.
- Added opt-in, fail-closed, read-only index integrity auditing with complete Stage 1 receipt, original source-block fingerprint (including zero-unit blocks), full bounded indexed-unit span/hash verification and mismatch/overflow rejection. Only dataset-snapshot integrity is assessed.
- Historical relation validity and source-version alignment explicitly remain UNDETERMINED; no automatic stale verdict, approval, migration or confirmed dependency. Synthetic offline tests only; production PostgreSQL, Windows GUI, customer PDFs and performance are NOT VERIFIED.

## v1.40 — Stage 2 body layout facts + Stage 3 relation-target diff revalidation (2026-10-23)

- Read-only `STRUCTURAL_LAYOUT` proves same section, direct parent/child section or consecutive extracted unit order using two independently verified source spans, document identity, version identity, page, Unicode offsets and SHA-256. `REJECTED` endpoints excluded; bounded 120-unit scan fails closed on overflow.
- No explicit SPEC code: reuse bounded body-backed semantic/SQL code candidates, then independently recheck both endpoints; fallback to existing exact/catalog title lookup. No global requirement scan.
- Join reviewed-for-source-fidelity `STRUCTURAL_REFERENCE` citations and hash-checked READY `CHAPTER_VERSION_DIFF` into `REFERENCE_CHANGE_REVALIDATION` only for exact same scope, target document, target version, specification code and matching section. No impact or stale inference; no approval/writes/migration.
- New offline contract/negative tests and guidance in `docs/ENGINEERING_v1.40_LAYOUT_DISCOVERY_AND_CHANGE_REVALIDATION_CN.md`; production database/Windows/real PDF/performance NOT VERIFIED.

## v1.39 — Stage 2 rule-verified section citations + Stage 3 extracted chapter diffs (2026-10-23)

- New **read-only** `STRUCTURAL_REFERENCE` retrieval path resolves source-exact same-clause SPEC/section citations to the selected target's PRIMARY/SPEC_BODY section, independently of the v1.33 review infrastructure. Confirms *only the literal reference*, not functional dependency, conformance or impact. Multiple requirement units in a section remain **one section-level citation**; no arbitrary requirement-to-requirement claim.
- New **read-only** `CHAPTER_VERSION_DIFF` path filters READY persisted body-to-body changes by explicit section/chapter, keyword and exact version pair, checking stored old/new SHA-256 before presentation. Missing history is **not** evidence of no change; this does not reconstruct unpersisted diffs.
- Current-version metadata constraints, two-end full content span verification, bounded SQL results and deterministic failure isolation; no automatic approvals, database migration, Qdrant rebuild or live production acceptance.
- 15 isolated adverse regression tests; source-only delivery. Production PostgreSQL 17, Windows GUI, real 508→550 PDF and performance thresholds remain **NOT VERIFIED**.

## v1.38 — Historical BUG-to-SPEC offline candidate indexing (2026-10-23)

- Opt-in QABUG-only tables for approved exact software→spec-version identity and UNREVIEWED candidate evidence; no cross-database FK or automated status approval.
- Bounded deterministic offline BUG phenomenon extraction/lexical top-N ranking with historical SPEC_BODY-only, immutable source text/span hash validation; dry-run default, distinct DB credentials.
- Online exact BUG-key lookup rechecks QABUG current source digest and historical spec-unit identity and body source; exposes display-only suggestions in Windows GUI / non-stream API but **never** in model context or evidence strength.
- Offline synthetic adverse tests cover version drift, coversheet and hash rejection. No live DB migration, Windows GUI runtime, Qdrant index change, 4B extraction or normative compliance verdict.


## v1.37 — Stage 2 database semantic gate and uncertain-commit reconciliation (2026-10-22)

- Added an **opt-in**, transaction-bound PostgreSQL 17 migration replacing the trusted relation write guard and creating a bounded, conservative server-side SPEC/section reference validator. Transitioning a `REFERENCES_SECTION` edge to CONFIRMED now checks the cited target and rejects multi-requirement target sections; it does not claim reference implies functional dependency.
- Pinned trusted SQL function bodies (UTF-8 prosrc SHA-256) in read-only preflight v3, and added a same-transaction client fail-closed write check when DB v1.37 is not installed. Existing v1.33 authenticated function, audit and grants remain intact.
- Exposed last audit watermark in review previews; added an opt-in read-only audit/identity/hash-based uncertain COMMIT reconciliation tool, always disallowing automatic retry.
- Added adverse-case synthetic regressions and operator deployment notes. **No live PostgreSQL 17 migration or end-to-end concurrency/permissions validation, Windows GUI test, or real 508/550 PDF verification was performed.**

## v1.36 — Stage 2 reference anchor fidelity and honest commit outcome (2026-10-22)

- Source-exact, clause-scoped specification/section reference anchors now power both proposal generation and persistence backfill. Ambiguous target sections are not converted into arbitrary requirement-to-requirement edges.
- Audit preview and RAG independently recheck that REFERENCES_SECTION genuinely names the selected target code and section; GUI, HTML graph and RAG expose exact anchors and state that reference != proven engineering dependency/impact.
- Offline bundle verification rederives exported anchors from both source text and target identity.
- GUI does not assert rollback when a network failure leaves a PostgreSQL COMMIT outcome uncertain; operator must re-query status and audit before retrying.
- Existing controlled review SQL function was not changed; this is an application/retrieval fidelity layer and does not claim to be a new DB-side authorization barrier. No live DB/Windows/real-PDF acceptance.

## v1.35 — Stage 2 full-database role authorization audit (2026-10-22)

- The Stage 2 PostgreSQL preflight now audits up to 500 non-superuser LOGIN roles capable of connecting to the target engineering DB, regardless of optional --app-role arguments; exceeding the cap is BLOCKED.
- Detects effective unregistered EXECUTE, indirect membership in the trusted function-owner role, table/column UPDATE, sensitive audit writes, TRIGGER/TRUNCATE and schema CREATE on the reviewed objects. Additional checks constrain guard function invoker/search_path attributes and function-owner privileged role flags.
- Preflight JSON version v2; review GUI and preflight have local lock/statement timeouts. No database migrations, automatic approvals, grants or data/index mutations; offline tests only, with live PostgreSQL and Windows verification pending.

## v1.34 — Stage 2 DB permission preflight & main GUI launcher (2026-10-21)

- Permanently fail-closed the legacy v1.31 self-declared actor direct UPDATE/INSERT API. The authenticated database function is the only supported writer.
- Main Settings GUI now opens the Stage 2 local reviewer console in a separate Python process with distinct PostgreSQL credentials; frozen EXE builds refuse unverified support.
- Added an opt-in read-only PostgreSQL configuration/grant preflight. It checks database/project identity, exact trigger target bindings, active full-operation triggers, SECURITY DEFINER/search_path, trusted NOLOGIN non-super function owner, no PUBLIC function EXECUTE, reviewer enrollment and effective reviewer/application role privileges including inherited grants. Returns `BLOCKED` with exit code 2 on a failed check, never auto-approves, changes schema or grants; report contains role identifiers but no engineering text.
- Added CLI regression tests with fake DB, negative privileges and transaction semantics. Live PostgreSQL and Windows GUI integration are still pending; offline preflight PASS is not production acceptance.



## v1.33 — Stage 2 trusted PostgreSQL review authority and local review GUI (2026-10-21)

- Added an isolated Tk Stage 2 reviewer workbench with full original two-ended evidence, explicit candidate/digest-prefix human acknowledgement, typed reason, explicit outcome and second confirmation. All display operations are READ ONLY; privileged writes are separate and never exposed through WebUI.
- Added opt-in PostgreSQL migration for dedicated SECURITY DEFINER review function: authenticated session_user allowlist, project/current-primary-version/source-span checks, NFKC hash and complete receipt checks, status precondition, relation/source row locks and one audit event in the same transaction. Database triggers reject normal-user relation updates and direct audit modification; audit events append only (subject to trusted DBA and schema owner exceptions).
- Review CLI now requires a dedicated DB reviewer login and independently previewed two-end SHA-256 digests instead of accepting a self-asserted --actor for writes. Reviewer connections need SELECT + EXECUTE, not direct UPDATE/INSERT. Legacy direct-DML helper remains in source for old code/tests but is blocked for non-owner roles after migration.
- Added explicit audit `identity_provenance` (`UNVERIFIED_LEGACY` vs authenticated `DB_SESSION_USER`). RAG requires the authenticated last event, a matching reviewed_by identity, installed function and two active guard triggers; historical self-asserted approvals cannot silently become RAG evidence. Review UI/report exposes the source of the last approval.
- Explicit unknown document_role values are treated as non-primary, never silently promoted to technical body evidence. The Stage 2 function requires the same trusted body labels and excludes coversheets.
- Offline synthetic regressions only: no production SQL migration executed, database grants audited, Windows GUI run, or 508→550 physical PDF acceptance. The v1.32 standalone snapshot is still read-only; never use it to authorize a state change.


## v1.32 — Stage 2 local content-node graph snapshot (2026-10-21)

- Added an offline, read-only graph exporter. Each source->target paragraph/requirement edge retains exact original text, file, project, version, page, section, offsets and text hash. Up to 40 content edges are displayed in an SVG topology; all validated edges up to the 200-record export cap have full HTML details.
- Revalidates candidate provenance using the existing fail-closed content-review loader, including complete backfill. Exports bad or STALE candidate IDs only as quarantine diagnostics, never their untrusted content. The export report never changes review state or RAG retrieval permissions.
- Added self-contained no-script, no-CDN HTML; JSON, checksum manifest and offline verification. Hashes are not signatures; verified outputs are historical, may include confidential primary specification text and require OS-level ACL controls.
- Added mocked database read-only transaction and output-integrity regressions. No live PostgreSQL/Windows GUI/real 508→550 validation, production authentication or schema upgrade. Direct continuation of v1.31 (main v1.30 baseline), not of absent v2.4.22.


## v1.31 — Stage 2 auditable content relation review gate (2026-10-21)

- Added `content_relation_review`: complete source/target provenance verification (current authoritative version, same engineering project, primary SPEC_BODY-equivalent document, original SQL content, Unicode offsets and SHA-256), human-only state transitions, optimistic previous state and a transaction-scoped audit append. Explicit dry-run queue / full evidence preview CLI.
- Content relation RAG now returns only **audited CONFIRMED** edges from configured engineering scopes, excluding QA/BUG and all unreviewed/rejected/stale proposals; exact source chunk offsets/hashes rechecked at retrieval time. Both endpoint locations and hashes are retained in citation metadata.
- Added adverse-case isolated regression coverage. No database schema migration, no reindex, no authenticated reviewer identity, no live PostgreSQL/Qdrant/Windows/real PDF certification. Base is repository v1.30 (2a41f0c), not an unprovided v2.4.22 build. OpenAI API contract unchanged.


## v1.30 — Internal-only multilingual Agent pre-Stage2 (2026-10-14)

- Added conservative multilingual/name/typo query alternatives for Bluetooth/BT, 仕様書/式样书/试样书, DCM, pairing/connection; LLM planner cannot introduce outbound web tools or model-authored SQL.
- Preserved explicit SPEC key and project QA/BUG routes while adding SQL identity discovery for fuzzy spec titles; cross-domain questions remain isolated to explicitly requested corpora.
- Changed all-empty enterprise retrieval to fixed abstention without calling Ollama; critical SQL/QA exceptions and absent Qdrant collections are no longer silent zero-hit results. Low-level warnings and traces are redacted.
- Added authenticated, read-only `/api/v1/health/knowledge` and offline `inspect_internal_knowledge_v130.py` with per-scope SQL reachability and Qdrant collection presence/count states. No secrets, source content, external proxy routing or database/index mutation.
- Shipped Open WebUI bypass audit instructions and a read-only PowerShell gateway check. External UI configuration, Windows GUI, PostgreSQL and original customer PDF are **not** certified by simulated tests. API compatibility version remains 1.20.


## v1.29 — Stage 1 installation and acceptance blockers (2026-10-14)

- Converted UTF-16 pinned `requirements.txt` to UTF-8 (with Windows-only `pywin32` marker); package versions unchanged.
- Made PostgreSQL driver load at connection time and format-specific ingestion pipelines load lazily.
- Replaced legacy collection-time database/PDF side effects with hermetic tests; GUI integration cases explicitly skip when real dependencies are absent.
- Added explicit opt-in Windows GUI import probe and PostgreSQL READ ONLY transaction/ROLLBACK probe with redacted JSON, an offline manifest checker, and a Windows Python 3.12 installer.
- Existing schema, Qdrant data and production database remain untouched. No real Windows/SQL/Gold sign-off is asserted.

# CHANGELOG

## v1.28 — Stage 1 release-readiness handoff, fail-closed evidence crosscheck (2026-10-14)

- Added pure local release preflight and evidence consistency gate: shipped critical files, static internal imports, interpreter dependencies, bounded real PDF SHA-256, source/document/project identity and incomplete native/SQL queue rejection. Outputs only a redacted acceptance checklist; always `stage1_production_acceptance=NOT_VERIFIED`.
- Added `scripts/stage1_release_readiness.py`, optional `scripts/run_stage1_readiness.ps1`, adversarial/negative regression tests, and explicit Windows/SQL/Gold sign-off procedure.
- Replaced legacy import-time hard-coded `input/test.pdf` smoke check with a self-contained two-page PyMuPDF pytest fixture. No synthetic business documents are shipped.
- Did not create new EXEs, authenticate reviewer identities, access any real PostgreSQL instance, or claim production semantic certification.

## v1.27 — Stage 1 structural human Gold and conservative holdout gates (2026-10-14)

- Added a reviewed structural Gold worksheet/metric engine for four-layer literal-source classification and exported section membership proxy, with strict report identity seals, private/metadata-only labels, no overwrite and a fail-closed reviewer declaration.
- Expanded the PDF native audit with a bounded exhaustive pair universe for **detected** adjacent-page geometry tables, including geometrically nonmatching pairs. This enables candidate Precision/Recall against manually reviewed truth within that detected-table universe; native table detection recall is not asserted.
- Combined measured structural gates with existing `stage1-gold-v3` requirement/scope holdout metrics. Undefined metrics, insufficient samples, missing Gold or incomplete source queues cannot pass gates. Reports never mark semantic/production acceptance.
- Added v1.27 synthetic regressions, PowerShell procedure and release docs; did not fabricate any real DCM/550 human Gold, nor use/write live SQL/Qdrant.


## v1.26 — Stage 1 live-source cross-layer DCM candidate audit (2026-10-14)

- Added one-command native PDF → exported Pipeline JSON → version-owned PostgreSQL `contents` → exact `ei_requirement_units` chain audit. It reuses the v1.24 authoritative source SHA-256, source digest and backfill-receipt checks inside `REPEATABLE READ READ ONLY`, and reruns the v1.25 physical PDF page/text audit in the same invocation.
- Joins only stable per-content pipeline indices and existing DCM evidence keys; rejects duplicate/ambiguous native anchors, section-not-in-exported-tree, wrong-page evidence, duplicate/missing SQL sources, unaligned Requirement Units and incomplete audit queues. No fuzzy text matching, state machine generation, relation approval or source text disclosure.
- Added binary rehash checks before/after SQL snapshot inspection, exclusive report creation, bounded audit output, secure password prompt/environment input and no implicit SQL/Qdrant writes.
- Added 16 synthetic tests including a full fake read-only SQL CLI transaction, PDF page drift, duplicate native lines/SQL rows, missing sections, scanned PDF, budget truncation, version hash gate and input/output safety.
- No schema migration, real DCM PDF Gold benchmark, actual PostgreSQL access, native PDF extraction guarantee, Windows GUI or EXE acceptance. Existing runtime API remains at v1.20 (unchanged).

## v1.25 — Stage 1 native PDF text-layer fidelity inspection (2026-10-14)

- Added explicit offline, privacy-preserving PDF → Pipeline JSON page-scoped text reconciliation. Exact unique single or contiguous native-line candidates include physical PDF locator metadata; repeated text, missing source, page drift, invalid page, weak anchor or section not in exported tree are always review-only, never accepted semantics.
- Added native PDF physical `pdf_page_count` so image-only trailing pages cannot fool page-range checks. Optional real PyMuPDF geometry yields conservative adjacent-page table continuation candidates; no header continuity or semantic merge approval.
- Added `scripts/audit_stage1_pdf_fidelity.py`, requiring existing replay-verified JSON diagnostic and explicit native PDF; bounded output, no private source text or path, no implicit overwrite, no SQL/Qdrant writes.
- Added 16 synthetic regressions using generated multi-page PDF files, including real near-edge grid geometry, native text ambiguity, mismatch, scan-without-text, section-tree mismatch, drift, strict safe CLI and report truncation.
- Environment: 610 collectable tests passed, 2 skipped. Seven older modules remain blocked from collection by missing `psycopg`/`ttkbootstrap` or `input/test.pdf`; no Windows integration, original customer DCM/Bluetooth Audio PDF Gold acceptance, or PostgreSQL live confirmation.

## v1.24 — Stage 1 DCM read-only SQL source trace (2026-10-14)

- Added strict replay-gated offline Pipeline JSON DCM evidence → immutable, version-owned PostgreSQL `contents` / `ei_requirement_units` source trace. Exact text, physical source page, section and Unicode offsets must match; duplicate or missing content never produces a fabricated SQL binding.
- Added operator-invoked `inspect_stage1_dcm_sql_trace.py`, with project-scoped 21MM/24MM database identity, native binary SHA-256 check against `documents.source_hash`, complete Stage1 backfill receipt, full live digest recheck, bounded read-only REPEATABLE READ transaction, fail-closed file output and no raw private text in report.
- On uniquely matched SQL Requirement Units, generated unapproved atomic and condition/action *candidate-status counts* using existing Stage 1 source-exact extractors; never approves state transitions, event-action scope, bilingual equivalence or PDF table semantics.
- Added 21 synthetic regressions including fake read-only SQL gateway, hash gate, drift, duplicate content, mismatched page/section, and scope integrity. 594 collectable tests passed; 2 skipped; 7 existing collection errors from `psycopg`, `ttkbootstrap`, or legacy `input/test.pdf` absent in this environment.
- No schema migration, SQL/Qdrant modification, native PDF semantic validation, actual PostgreSQL integration or Windows EXE rebuild.


## v1.23 — Stage 1 DCM source-phase and scope review (2026-10-14)

- Introduced bounded, read-only DCM lifecycle lexical diagnostics on existing authorized pipeline JSON, covering initial state, explicit DCM communication before/after, success, failure, negative UI/APP state and exception cues without inferring state transitions or condition/action inheritance.
- Added source-exact, redacted `DCM_SOURCE` evidence pointers and same-section `DCM_RELATED_UI_SOURCE` context pointers for explicit Telematics/APP button initial-state lines; all verified by the existing offline integrity replay. Same-line multiple temporal cues and same-section multi-phase mentions generate explicit human-review warnings; primary data is never modified.
- Preserved Stage 1 bilingual normative probes, section-reference warnings and the atomic two-page PDF fragment pointer rules. For DCM documents evidence priority is given to DCM-specific source lines under bounded export budgets.
- Added 19 synthetic DCM lifecycle regressions. Available tests: 573 passed, 2 skipped; 7 collection errors due to unavailable environment modules (`psycopg`, `ttkbootstrap`) or legacy `input/test.pdf`.
- No SQL migration, Qdrant update, native PDF authenticity or real Telematics/DCM Gold evaluation; no Windows EXE build.

## v1.22 — Stage 1 auditable offline evidence pointers (2026-10-14)

- Added `stage1_review_evidence.py`: deterministic redacted pointers to exported pipeline JSON text ranges with source SHA-256, evidence SHA-256, Unicode offsets, document hashes, and explicit `UNREVIEWED` status. Never asserts native file authentication or approved engineering semantics.
- Attached review pointers to Japanese/English normative source lines, unresolved scope candidates, cross-reference-as-section warnings, and conservative adjacent PDF page text-table fragments. Cross-page pairs are emitted atomically under evidence budget.
- Added offline `scripts/verify_stage1_pipeline_evidence.py`: replays complete JSON diagnostic, compares reports and refuses source/offset drift without disclosing source text. Read-only; no SQL, Qdrant or network use.
- Eliminated repeated per-section scans of every source content record, flagged duplicate section identities, redacted non-numeric section IDs / unrecognised source formats and enforced additional resource budgets.
- Added fifteen synthetic targeted regressions. Test evidence: 232 Stage 1 focused passed; 553 collectable tests passed and 2 skipped, with seven separate collection errors caused by missing `psycopg`/`ttkbootstrap` or the legacy `input/test.pdf` fixture. Windows/real PDF/Gold/database acceptance pending.

## v1.21 — Stage 1 Bluetooth Audio semantics and evidence-bound review (2026-10-14)

- Hardened `TitleDetector` for reference-cell suffixes and added hashed, source-text-free `SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL` diagnostic for section nodes that match visible numeric cross-reference lines. Existing exports remain unchanged; source headings are never removed by a report.
- Added `stage1_semantics.py`: Japanese/English/mixed normative source-unit counters, bounded condition/action review sampling, and conservative adjacent-page flattened table fragment discovery for PDF pipeline JSON. It never constructs bilingual equivalence pairs or joins PDF tables.
- Improved Japanese normative/conditional cues (`する必要がある`, `接続時、`, `時は`) and guarded multi-action and inherited conditional scopes; unresolved scopes no longer retain misleading condition/action spans.
- Marked multiple geometry-compatible native PDF cross-page table matches with `MULTIPLE_COMPATIBLE_TABLES_REVIEW`; every candidate remains human-review-only and has no verified semantic header.
- Added eleven synthetic Bluetooth Audio-focused regressions; retained v1.20 tests and CLI read-only privacy contract.
- Verification: 222 Stage1/analyzer/title/release tests passed; 539 passed, 2 skipped when omitting seven test modules blocked by missing unrelated external runtime dependencies or the absent `input/test.pdf` fixture. Full Windows/real-PDF/SQL integration acceptance is not claimed.


## v1.20 — Stage 1 Real Pipeline-JSON Quality and Evidence Isolation

- Inspected two user-provided processed JSON examples offline (supporting cover workbook and primary Bluetooth Audio PDF) without distributing the private source files.
- Prevented numbered cross-reference text ending in Japanese reference markers from being promoted into Section headings by `TitleDetector`. Kept legitimate headings such as reference methods allowed; synthetic regression added. This affects **future native re-import**, not previously stored SQL or exported JSON.
- Added a read-only, bounded, privacy-preserving `scripts/review_stage1_pipeline_json.py` command for existing pipeline exports. Checks chapter pollution, section numbering discontinuities, supporting-document indexing policy, file-versus-sheet version conflict, unresolved primary spec code, and PDF table flattening / missing native offsets.
- Added optional cross-file **manual review-only** identity candidates when same project, matching version, filename topic, and unique Cover Sheet visible code agree. No automatic assignment or SQL/Qdrant modification. Reports contain hashes/locations/codes but neither raw text nor source filenames.
- Added synthetic CLI/security and exact-heading regressions; preserved old filtered `stage1-filtered-samples-v1` path for manually redacted excerpts. Full Windows/PostgreSQL and original PDF/XLSX reparse acceptance remains pending.


## v1.19 — Windows GUI startup packaging integrity correction

- Restored `app/output/__init__.py` and `app/output/project_output_router.py` from the previously shipping v1.05 implementation. They were inadvertently omitted from later source ZIP cleanup, preventing all v1.18 GUI startups with `ModuleNotFoundError: No module named app.output`.
- Added a dependency-free internal-source-import preflight `scripts/verify_runtime_package.py`, invoked by `run.ps1` before loading the GUI. This catches missing shipped `app.*` modules in the full application source and scripts; it cannot replace Windows dependency or integration testing.
- Added test coverage for restored JSON-only project separation and missing-module detection. Preserved v1.18 Stage 1 filtered JSON diagnostics, SQL/Qdrant compatibility and existing data.
- This is a **source-only** complete release; no database migrations or vector rebuilds. Actual Windows GUI start remains to be verified on the target computer.


## v1.18 — Stage 1 Offline Filtered-JSON Diagnostic and Evidence Isolation

- Added explicit `stage1-filtered-samples-v1` input manifest (transformation and offset semantics) for authorized, filtered or synthetic excerpts. Original source binding is **never** inferred from sanitized text or a supplied SHA digest.
- Added strictly local Stage 1 extraction/cue/scope/table diagnostics with bounded JSON size, per-case budgets, scoped identity validation and exact supplied-text offsets. No online LLM, SQL write, vector store, or auto-approval.
- Added metadata-only diagnostic report CLI with no raw engineering text, fail-closed identity/hash checks, privacy-pattern counts, no-overwrite output and no Gold metrics without human labels. Privacy scanner does **not** certify de-identification.
- Added synthetic regression tests, schema template and Windows operator notes. Kept all v1.17 reviewed Gold and relation/version features intact; no new migration.
- Genuine 508/550 accuracy, complex native table semantics and Windows/PostgreSQL integration remain **not independently verified**.


## v1.17 — Stage 1 independently-reviewed holdout workflow and stratified quality evaluation

- Added `stage1-gold-v3` with explicit 21MM/24MM project scope, DOCX/PDF body/table provenance, development/holdout splits, bounded complexity tags, immutable source fingerprint and per-label SHA-256 evidence seals.
- Gold scoring refuses incomplete, unreviewed, overlapping, drifting or reclassified samples; independent human reviewer and annotator must be distinct (identity is declared, not authenticated). Legacy v1/v2 readers remain functional.
- Added held-out exact-span F1 for requirements and conditions/actions/exceptions, per-source-kind/project/complexity breakdowns, per-role metrics, bounded redacted error offsets, and sample-count/source-kind/complexity CI gates plus per-condition/exception/action thresholds; absent positive examples yield **no F1**, never a perfect score.
- Added explicit opt-in private v3 worksheet preparation, redacted per-case review digest preview and a local Stage 1 GUI quality report tab. No cloud, model call, DB migration or automatic rule approval.
- Maintained v1.16 physical table-header evidence and review ledger. Genuine 508/550 accuracy, independent reviewer identity, merged/cross-page semantics and real Windows/PostgreSQL approval remain **unverified**.


## v1.16 — Stage 1 human gold v2 and explicit native table header review

- New source-anchored, independently human-labelled requirement and scope exact-span evaluation (`stage1-gold-v2`), with explicit missing-scope handling and optional CI F1 thresholds. Backwards-compatible `stage1-gold-v1`.
- New local-only, opt-in unlabelled gold worksheet preparation with source SHA verification, bounded size and no overwrite. No fake 508/550 acceptance sample included.
- New explicit physical multi-row table header-to-column-to-data evidence preview for DOCX/PDF, with unbound/merge/cross-page warnings; never approves semantic relationships.
- Integrated preview into read-only PostgreSQL source check and local reviewer GUI, with verified whole-proposal source coverage before showing header matrix.
- No database migrations or live data changes. Real 508/550 human-labelled metrics and Windows/PostgreSQL end-to-end approval remain pending.

## v1.15 — Stage 1 Review-Native Evidence Alignment

- Added explicit, operator-triggered DOCX/PDF-to-SQL source comparison inside the standalone local reviewer GUI: dual-panel original text/physical-cell anchors, highlighted exact source intervals, verified document SHA-256 and fixed project/version snapshot.
- Joined existing SCOPE and TABLE review proposals with uniquely matched native text anchors; support DOCX body paragraphs, DOCX grid cells and PDF native text / conservative table geometry. Missing/ambiguous anchors and cross-page continuity remain review-only, never automatically approved.
- Added bounded read-only PostgreSQL snapshot loader, payload/source digest consistency checks, UI display budgets, an opt-in CLI `--review-proposal-key` with redacted output, and independent synthetic integrity/regression tests; no migration and no changes to existing Qdrant/Open WebUI interfaces.
- Known acceptance gap: no verified 508/550 gold dataset or actual Windows/PostgreSQL GUI test; physical coordinates cannot prove engineering semantics.


## v1.14 — Stage 1 Local Review Workbench & Provenance Quality

- Added a local-only Tkinter reviewer workbench launched from the main Settings/PostgreSQL Actions area or a standalone script. Scoped read-only proposal queue, source payload/decision history, explicit human confirmation, current-event CAS and controlled PostgreSQL transaction. No reviewer API exposed to Open WebUI.
- Added physical DOCX cell and PDF geometry-to-SQL evidence-only summaries, including source IDs and UTF-8 checksums without logging private text; merged header, cross-page semantics and ambiguous bindings remain review-only.
- Added deterministic gold-labelled requirement span evaluation with SHA256 source pinning, bounded input and exact-offset Precision/Recall/F1. No real 508/550 accuracy claim without a verified labelled dataset.
- Added test coverage for review queue access scoping, tampering, history conflicts, physical binding accuracy and evaluation failure modes. No new DB migration or online model calls.


## v1.13 — Stage 1 Source-anchored Human Review Ledger

- Added deterministic SCOPE/TABLE proposal generation with exact original condition/exception/action spans, SHA256 evidence and undecided precedence. Ambiguous scopes never become automatically approved rules.
- Added safe human assertions for explicitly anchored condition/action and exception/action pairs and table header row IDs; reject inconsistent table columns and ambiguous separators.
- Added additive, explicitly applied Stage1 v4 PostgreSQL migration for immutable proposals, backfill receipts and append-only review events without changing previously reviewed source requirements or graph relations.
- Added offline dry-run/apply backfill, metadata-only listing, opt-in evidence display, and human review CLI with source hash, project scope, previous-event CAS checks and SERIALIZABLE transactions.
- Preserved normal RAG API and Qdrant operations. Extended synthetic integrity and regression tests. Official 508/550 acceptance and Windows live PostgreSQL migration have not been completed.

## v1.12 — Stage 1 Source Binding & Cross-page Review Hints

- Added read-only, strict native DOCX/PDF to version-owned PostgreSQL contents source-span candidate mapping. Accepts only authoritative source binary SHA-256, full source-backfill integrity, exact unique Unicode source strings and, where present, consistent native PDF page numbers.
- Added DOCX top-level body-paragraph identities (physical body order + SHA-256) alongside existing OOXML table cells; omitted duplicate vertical-merge continuation anchors.
- Bounded native-prefix automaton scans each SQL source snapshot once. Duplicate SQL occurrences and multiple physical anchors mapping to the same occurrence are explicitly ambiguous; never promoted into evidence.
- Added optional PDF adjacent-page table geometry *review candidates* only when edge proximity and column boundaries align; never merges tables, infers merged headers or assigns normative semantics.
- Added read-only SQL reviewer command, conservative privacy defaults and resource limits; no database migration, SQL write, Qdrant rebuild, model download or change to Open WebUI API contract.
- Added synthetic provenance, ambiguity, page mismatch, adjacent-page and privacy tests. Windows live PostgreSQL, official 508/550 evidence acceptance and condition/exception priority reviews still outstanding.

## v1.11 — Stage 1 Native Layout & Condition Scope Evidence

- Added read-only, bounded native DOCX OOXML table grid inspection (horizontal/vertical merges, nested/omitted-cell review flags); DOCX physical pages remain unknown.
- Added PDF page-specific text-line rectangles and opt-in geometry-only table candidates; no inferred row/header meaning, OCR, cross-page merging, or SQL source IDs.
- Added conservative, multilingual conditional/exception/action source-span candidates with explicit unknown scope, no implicit cross-atom inheritance and exact Unicode coordinate verification.
- Added offline developer CLI reports with source-text redaction by default, explicit no-overwrite output, resource limits and privacy controls.
- Kept all previous RAG/Open WebUI/PostgreSQL/Qdrant, Stage1 v2/v3 stores and review decisions unchanged; no database migration or online models.
- Added synthetic regression cases and actual read-only DOCX parsing smoke test; real 508/550 coverage, native-to-SQL provenance and Windows end-to-end acceptance remain pending.

## v1.10 — Stage 1 Atomic Requirements & Table Evidence

- Added deterministic, provenance-exact, review-only atomic requirement candidates; conservative splitting and condition-scope warnings.
- Added table header/column source evidence only for explicit Markdown header separators; unknown/merged/cross-page geometry remains unresolved.
- Added Stage1 v3 derivative PostgreSQL tables with immutable receipt, source fingerprint recalc, batch insert, drift verification and non-destructive review handling.
- Added default dry-run, backup-gated Stage1 v3 backfill script and fully local inspector mode.
- Preserved RAG, QA/BUG, current-version/history and Open WebUI API; no migrations at startup.
- Added Stage1 v3 unit/contract/regression tests; real Windows DB and complex document validation pending.

# CHANGELOG

## v1.09 — Stage 1 Requirement Foundation & Evidence Integrity

- Added verbatim source evidence quality verification, exact source cue and table-cell coordinates, multilingual condition/modality and engineering quantity hints.
- Fixed identifier trailing punctuation and generic Chinese table-header false positives; source-preserving boundaries with fail-closed limits.
- Added non-destructive v2 signal enrichment sidecar + explicit additive SQL migration for old reviewed v1 records.
- Added read-only local inspection tool and expanded provenance/compatibility tests; no external LLM/network dependency.
- Stage 1 requires human validation on real document/table samples; no claim that atomic semantic conditions or Stage 2–7 are complete.

## v1.08 — Local Backend Resilience & Operational Hardening

- Bounded chat concurrency with configurable per-process admission and HTTP 429/Retry-After overload response; protect GPU/local LLM resources against parallel request spikes.
- Streaming and nonstreaming lease lifecycle: release exactly once on normal completion, error, disconnect or response cleanup.
- Added authenticated aggregate chat counters and separate lightweight embedding readiness (no claim of SQL/Qdrant/Ollama availability from model discovery alone).
- Added opt-in, user-login-session-independent-of-GUI Windows watchdog with exclusive lock, bounded restart attempts, rotating logs, status heartbeat, GUI toggle and reversible PowerShell management. Does not implement host-level failover.
- Added opt-in, verified PostgreSQL dump utility and read-only network/private-port readiness audit; existing Qdrant volume and SQL data untouched.
- Added focused concurrency, failure, watchdog and security tests; retained v1.07 requirement/relationship/version integrity.
- Explicitly document multi-user Backend RBAC, internal HTTPS, restore drill and redundant host as production prerequisites.


## v1.07 — Engineering Intelligence Integrity & Reliability

- Refused silent requirement-unit extraction truncation; a source chunk above the reviewed limit now fails with an explicit error.
- Added version backfill receipts including source-chunk/requirement counts, maximum source Content ID, and source SHA-256 fingerprint; sources changed after earlier backfill fail closed.
- Historical requirement mapping and content relation extraction now require complete indexed versions, including content rows that yield zero requirement units.
- Prevented false paragraph-level links for explicit references to a section with multiple candidate paragraphs; added a persistent unresolved-section anchor registry and read-only API endpoint.
- Added indices supporting version-order and target-section probes; reduced duplicate exact-text historical alignment from repeated scans to hash/deque matching.
- Scoped explicit QA/BUG API operations to compatible knowledge scopes and kept localhost bearer authorization.
- Added read-only integrity diagnostic, revised backup/migration/operator documentation, excluded the synthetic 1001/2007 DOCX fixtures, and synchronized UI / API version IDs.
- Kept legacy SQL/Qdrant business data and Open WebUI connection unchanged. This release does not claim to have implemented the remaining advanced Agent / GraphRAG roadmap phases.

## v1.06 — Engineering Intelligence Foundation (incremental release)

- Add local-only, additive v7 requirement-span schema: source offsets, version ownership and immutable evidence references.
- Add conservative, review-gated paragraph-level cross-specification reference candidates with two original source spans.
- Add deterministic, bounded requirement-unit alignment across immutable versions and explicit human-review audit CLI.
- Add read-only, authenticated REST inspection endpoints for requirements, content-level relations and version mappings.
- Extend existing AI relation/version tools with optional v7 evidence, while keeping legacy paths working without migration.
- Add validated workflow graph blueprint, limited multilingual terminology expansion, and explicit cross-domain QA/BUG routing.
- Add backup-before-migration PowerShell 5.1 entrypoint and bounded, opt-in incremental backfill.
- Note: subsequent roadmap stages (semantic conflict engine, sparse/rerank, durable parallel DAG, real QA-to-requirement links) remain outstanding and are not claimed complete.



## v1.05

- Added asynchronous, authenticated Backend auto-launch from GUI startup, using a detached localhost-only Python process and startup logs; UI closure no longer hides the RAG model.
- Added Windows current-user login autorun toggle with reversible HKCU Run entry; intentionally not a pre-login system service.
- Added developer Settings one-click approved Wi-Fi LAN portproxy and a tightly scoped firewall rule for Open WebUI **3000 only**; opt-in UAC and reversible disable operation; current IP rediscovery and status reporting.
- Fail-closed LAN enable if PostgreSQL, Qdrant, Ollama or Backend have non-loopback listeners; preserves existing Open WebUI container, Qdrant volumes, databases and API key.
- Added local deployment tests, Windows launcher, PowerShell 5.1 ASCII configuration script and v1.05 deployment guidance. Production LAN HTTP still requires internal HTTPS and knowledge-domain authorization.

## v1.04

- Added independent localhost-only FastAPI backend for Docker Open WebUI; service lifecycle no longer depends on GUI.
- Reused authoritative EnterpriseRAGService including fixed tools for specifications, QA/BUG, immutable version history and engineering relations; read-only retrieval endpoint.
- Added Bearer-token gated OpenAI-compatible `/v1/models` and streaming/non-streaming chat completions; exposes one governed RAG model.
- Added external WebUI URL/port, default-browser launcher, clipboard copy, preferred model display, nonblocking local service checks in main Settings and AI Settings.
- Enforced local dependency endpoints and offline embedding mode; default legacy static API token removed; desktop-embedded API auto-start disabled.
- Added opt-in Qdrant port-rebinding script preserving `qdrant_storage` and Windows PowerShell diagnostics.
- Preserved v1.03 RAG, primary-body priority, QA/BUG schema, historical version behavior, ingestion and sync.

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

