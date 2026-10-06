from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "upgrade_chunk_evidence_mapping_v6_1.sql"
).read_text(encoding="utf-8")


def test_exact_join_is_content_id_to_contents_id():
    normalized = " ".join(SQL.lower().split())
    assert "left join public.contents as c on c.id = re.content_id" in normalized


def test_no_fuzzy_mapping_fallback_in_view():
    lowered = SQL.lower()
    forbidden = (
        "similarity(",
        "levenshtein",
        "word_similarity",
        "content ilike",
        "evidence_text ilike",
        "c.chunk_index = re.",
    )
    for token in forbidden:
        assert token not in lowered


def test_snapshot_and_audit_contract_present():
    required = (
        "chunk_content_id_snapshot",
        "chunk_content_hash_snapshot",
        "capture_relation_evidence_chunk_snapshot",
        "relation_evidence_chunk_map",
        "CONTENT_CHANGED",
        "IDENTITY_CHANGED",
        "CHUNK_MISSING",
        "UNMAPPED",
        "EXACT",
    )
    for token in required:
        assert token in SQL
