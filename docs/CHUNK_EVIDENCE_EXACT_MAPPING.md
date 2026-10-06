# Chunk / Evidence Exact Mapping v1

## Objective

This layer closes the traceability gap between Engineering Relation Evidence and the canonical RAG Chunk.

The mapping rule is intentionally strict:

```text
relation_evidence.content_id
          ↓ exact FK / join
      contents.id
          ↓
Document / Chapter / Section / Page / Chunk / Hash / Full Text
```

No fallback mapping is allowed by:

- `chunk_index`
- page number
- section title
- evidence text similarity
- substring matching
- vector similarity

Those fields can be duplicated or can change presentation semantics. `contents.id` is the stable canonical Chunk identity.

## Audit snapshot

At Evidence write time the database captures:

- Chunk content ID (historical identity)
- Chunk document ID
- Chunk chapter ID
- Chunk section ID
- Chunk page number
- Chunk index
- Chunk sort order
- Chunk content hash
- Capture timestamp

This creates two layers of truth:

1. `content_id -> contents.id` for the authoritative mapping.
2. Snapshot invariants for detecting a later identity/content mutation.

## Mapping statuses

| Status | Meaning |
|---|---|
| `EXACT` | content_id resolves and all captured invariants still match |
| `CONTENT_CHANGED` | same contents.id exists, but content_hash changed |
| `IDENTITY_CHANGED` | same contents.id exists, but document/section/page/index/order changed |
| `CHUNK_MISSING` | historical content_id no longer resolves |
| `UNMAPPED` | Evidence has no content_id |

A non-EXACT row is **never automatically remapped**. It must remain visible for audit/reconciliation.

## Delete behavior

The FK uses `ON DELETE SET NULL` so deleting a Chunk cannot cascade-delete Evidence. Engineering relation evidence remains available as an immutable audit record, while the mapping becomes `UNMAPPED` after deletion.

## Schema versioning

This patch does not change `document_ingestion_rag = v6`. It records an additive extension component:

```text
document_ingestion_chunk_evidence_mapping = 1
chunk-evidence-exact-v1
```

This avoids conflating the mapping hardening patch with a future core RAG schema revision.
