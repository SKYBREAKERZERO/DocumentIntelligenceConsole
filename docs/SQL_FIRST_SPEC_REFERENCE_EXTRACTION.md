# SQL-first Cross-Specification Reference Extraction (v5.2.1)

## Goal

Resolve references from one specification to another **before Qdrant** and without making an LLM/vector result authoritative.

Authoritative path:

```text
DOCX/PDF -> parser -> contents -> deterministic reference extraction
         -> PostgreSQL specification registry -> spec_relations
         -> relation_evidence.content_id -> contents.id
```

Qdrant is not used by this extractor (`qdrant_used=false`).

## What v5.2.1 improves

1. Exact specification code matching, including NFKC/full-width digits and Unicode dash normalization.
2. Exact canonical-name matching.
3. Exact aliases from `specifications.metadata` keys:
   - `reference_aliases`
   - `aliases`
   - `document_aliases`
   - `legacy_names`
   - `legacy_titles`
4. Reference-section awareness for headings such as:
   - References / Referenced Documents / Applicable Documents / Related Documents
   - 参照文書 / 適用文書 / 関連文書 / 引用文書
   - 引用文档 / 适用文档 / 相关文档 / 关联文档 / 参考文件
5. Reference-table rows such as `2007 | User Profile Communication Interface Spec | Rev2.1` can be resolved even when the word `Specification` is not repeated in the row.
6. Quoted titles (`《...》`, `「...」`, `『...』`, `"..."`) are resolved by exact registry title/alias.
7. Unknown explicit code/title references are preserved as `UNRESOLVED` rather than discarded.
8. Multiple exact matches become `AMBIGUOUS`; the system does not guess.
9. Internal references such as `本书第5.1节` are not turned into external spec edges.
10. ISO/IEC/IEEE/JIS/SAE/DIN/EN/ASTM standard references are excluded from the specification graph.
11. Every relation keeps exact evidence via `relation_evidence.content_id -> contents.id`.

## Resolution order

```text
Exact specification code
-> Exact canonical name
-> Exact configured alias
-> Explicit code in reference syntax
-> Code row under a reference-document section
-> Exact quoted title/alias
-> UNRESOLVED / AMBIGUOUS
```

No fuzzy or vector candidate is automatically promoted to `CONFIRMED`.

## Existing database upgrade

Run once against each RAG Schema v6 database:

```powershell
psql -h 127.0.0.1 -U postgres -d <database> -f database\upgrade_spec_reference_sql_v6_2.sql
psql -h 127.0.0.1 -U postgres -d <database> -f database\verify_spec_reference_sql_v6_2.sql
```

The upgrade is non-destructive and adds only normalization helpers, indexes, and audit views.

## Backfill existing versions

Use the existing relation backfill with `--force` so v1 extraction generations are replaced by v2 current generations:

```powershell
$env:POSTGRES_PASSWORD="<password>"
python scripts\backfill_engineering_relations.py --database <database> --project 24MM --force
```

Then audit:

```powershell
python scripts\audit_spec_references.py --database <database> --project 24MM
python scripts\audit_spec_references.py --database <database> --project 24MM --unresolved-only
```

## Useful SQL

Resolved/current references:

```sql
SELECT *
FROM public.spec_reference_current_edges
ORDER BY source_specification_id, relation_id;
```

Unresolved/ambiguous references:

```sql
SELECT *
FROM public.spec_reference_unresolved_current
ORDER BY source_specification_id, relation_id;
```

Evidence/chunk trace:

```sql
SELECT
    d.source_specification_key,
    d.target_specification_name,
    d.target_external_key,
    d.extraction_method,
    d.status,
    d.content_id,
    d.section_title,
    d.evidence_text
FROM public.relation_evidence_detail d
WHERE d.relation_type = 'REFERENCES'
ORDER BY d.source_specification_id, d.relation_id, d.evidence_id;
```

## Expected ingestion behavior

When specification `1001` references `2007` and `1001` is imported first:

```text
1001 -> SPEC:2007   UNRESOLVED
```

After `2007` is imported, `reconcile_unresolved()` performs an exact SQL-registry reconciliation:

```text
1001 -> 2007        CONFIRMED
```

No re-embedding is required for this identity resolution.
