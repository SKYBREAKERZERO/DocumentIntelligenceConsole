"""Deterministic Stage-1 quality findings. Never asserts an engineering fact.

All findings refer to already extracted, verbatim RequirementUnits; no LLM,
network, database or machine-specific resources are needed for an audit.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from .requirements import RequirementUnit, validate_extracted_units


@dataclass(frozen=True, slots=True)
class QualityFinding:
    code: str
    severity: str
    unit_key: str
    detail: str


def review_requirement_units(
    text: str, units: tuple[RequirementUnit, ...], *,
    long_unit_chars: int = 2_000,
) -> tuple[QualityFinding, ...]:
    """Return review candidates, raising on incomplete/altered source coverage.

    Warnings are not automatic rejections: tables, multiple modals and blank
    pages may be legitimate features of an engineering specification.
    """
    if long_unit_chars < 1:
        raise ValueError("long_unit_chars must be positive")
    validate_extracted_units(text, units)
    findings: list[QualityFinding] = []
    for unit in units:
        if len(unit.text) > long_unit_chars:
            findings.append(QualityFinding(
                "LONG_SOURCE_UNIT", "REVIEW", unit.unit_key,
                f"{len(unit.text)} characters in one physical source unit; verify upstream extraction",
            ))
        if unit.unit_type == "TABLE_ROW" and unit.signals.normative:
            findings.append(QualityFinding(
                "TABLE_RULE_REVIEW", "REVIEW", unit.unit_key,
                "Normative cue in a table row; inspect header/column meaning before interpreting",
            ))
        if sum(kind == "NORMATIVE" for kind, *_ in unit.signals.cue_spans) > 1:
            findings.append(QualityFinding(
                "MULTIPLE_NORMATIVE_CUES", "REVIEW", unit.unit_key,
                "Multiple normative cues in a single source unit; may require atomic clause review",
            ))
    return tuple(findings)


def identify_repeated_requirement_ids(
    units: tuple[RequirementUnit, ...],
) -> dict[str, tuple[str, ...]]:
    """Identify repeated explicit IDs; repetition alone is *not* an error.

    An ID mentioned in two different units may be a valid reference, not two
    definitions. Do not automatically merge or approve an identity from this.
    """
    keys: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        for identifier in unit.signals.identifiers:
            keys[identifier].add(unit.unit_key)
    return {
        identifier: tuple(sorted(unit_keys))
        for identifier, unit_keys in sorted(keys.items()) if len(unit_keys) > 1
    }


def summarize_requirement_units(
    units: tuple[RequirementUnit, ...],
    findings: tuple[QualityFinding, ...],
) -> dict:
    return {
        "units": len(units),
        "unit_types": dict(sorted(Counter(item.unit_type for item in units).items())),
        "source_structures": dict(sorted(Counter(item.signals.structural_kind for item in units).items())),
        "modalities": dict(sorted(Counter(item.signals.modality for item in units).items())),
        "condition_cue_units": sum(item.signals.conditional for item in units),
        "exception_cue_units": sum(item.signals.exception for item in units),
        "time_cue_units": sum(bool(item.signals.timing) for item in units),
        "numeric_cue_units": sum(bool(item.signals.quantity_cues) for item in units),
        "findings": len(findings),
        "finding_codes": dict(sorted(Counter(f.code for f in findings).items())),
        "repeated_explicit_ids": len(identify_repeated_requirement_ids(units)),
    }


def review_stage1_v3(units: tuple[RequirementUnit, ...]) -> tuple[QualityFinding, ...]:
    """Conservative review queue for derived atomic/table evidence.

    Does not turn proposed relations into approved engineering facts.
    """
    from .atomic import extract_atomic_candidates
    from .tables import extract_table_blocks

    findings: list[QualityFinding] = []
    for unit in units:
        for candidate in extract_atomic_candidates(unit):
            for reason in candidate.review_reasons:
                if reason in ("CONDITION_OR_EXCEPTION_SCOPE_UNRESOLVED", "COMPOUND_BOUNDARY_NOT_SAFE",
                              "MULTIPLE_NORMATIVE_CUES", "STRUCTURAL_SCOPE_UNRESOLVED"):
                    findings.append(QualityFinding(
                        "ATOMIC_" + reason, "REVIEW", unit.unit_key,
                        "Clause boundaries/scope need source-level engineering review",
                    ))
    for table in extract_table_blocks(units):
        for reason in table.review_reasons:
            findings.append(QualityFinding(
                "TABLE_" + reason, "REVIEW", table.rows[0].unit_key,
                "Table header or physical cell layout is not fully verified",
            ))
    return tuple(findings)
