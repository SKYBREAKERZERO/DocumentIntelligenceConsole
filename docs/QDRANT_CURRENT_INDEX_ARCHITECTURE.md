# Qdrant Current Index Architecture

## Authority boundary

```text
PostgreSQL                    Qdrant
Source of Truth               Derived Retrieval Index
--------------                -----------------------
rag_current_chunks   --->     *_current_chunks
contents.id          <---     payload.content_id
spec/version metadata         retrieval filters
relation/evidence SQL         NOT inferred by vector similarity
```

## Normal QA path

```text
Question
  -> SQL relation expansion / scope constraints
  -> Qdrant current semantic retrieval
  -> payload.content_id
  -> PostgreSQL contents.id
  -> exact chunk / specification / version / section
  -> evidence-grounded answer
```

## Why only current chunks
Historical chunks are preserved in PostgreSQL for audit and version comparison but are excluded from the default semantic collection so a normal QA answer cannot silently mix obsolete revisions with the active specification.

## Incremental sync
`index_hash` changes when the source text, key specification metadata, embedding model, index version or vector dimension changes. Only New + Changed rows are embedded. Known Stale rows are deleted from Qdrant.

## Full rebuild
Full rebuild deletes and recreates the selected current collection, clears matching sync state, then embeds all current source rows. It is the deterministic drift-repair operation.
