"""Conservative local conditional/action evidence for Stage 1 atomic candidates.

These are *reviewable spans*, never executable engineering rules. The module
only links a prefix condition to an action when punctuation and source cues
provide a reproducible boundary. Cross-atom inheritance is never assumed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .atomic import AtomicCandidate
from .requirements import RequirementUnit, _source_signals

SCOPE_VERSION = "condition-scope-candidates-v1"
_PREFIX_END = ",，、:："


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start_in_content: int
    end_in_content: int
    exact_text: str
    role: str


@dataclass(frozen=True, slots=True)
class RequirementScopeEvidence:
    candidate_key: str
    parent_unit_key: str
    modality: str
    status: str  # EXPLICIT_PREFIX_CANDIDATE | ACTION_ONLY | UNRESOLVED
    condition: SourceSpan | None
    exception: SourceSpan | None
    action: SourceSpan | None
    review_reasons: tuple[str, ...]


def _span(atom: AtomicCandidate, left: int, right: int, role: str) -> SourceSpan | None:
    if not (0 <= left < right <= len(atom.text)):
        return None
    source = atom.text[left:right]
    if not source.strip():
        return None
    return SourceSpan(atom.start_in_content + left, atom.start_in_content + right, source, role)


def extract_requirement_scope(
    unit: RequirementUnit, atom: AtomicCandidate,
) -> RequirementScopeEvidence:
    if atom.parent_unit_key != unit.unit_key or atom.content_id != unit.content_id:
        raise ValueError("Scope atom belongs to a different source unit")
    if unit.text[atom.start_in_unit:atom.end_in_unit] != atom.text:
        raise ValueError("Scope atom does not match verbatim source")
    signals = _source_signals(atom.text)
    cues = signals.cue_spans
    reasons: set[str] = set(atom.review_reasons)
    normative_cues = [(s, e) for k, s, e, _ in cues if k == "NORMATIVE"]
    normative = [s for s, _ in normative_cues]
    # Lexical matches such as Chinese 应当 also contain 当; do not interpret
    # those overlapping letters as a new conditional clause.
    def inside_normative(start: int, end: int) -> bool:
        return any(start < ne and end > ns for ns, ne in normative_cues)
    conditions = [s for k, s, e, _ in cues if k == "CONDITION" and not inside_normative(s, e)]
    exceptions = [s for k, s, e, _ in cues if k == "EXCEPTION" and not inside_normative(s, e)]
    # "unless" is tagged as both conditional and exceptional by the lexical
    # recognizer. Count the single source marker only once.
    conditions = [s for s in conditions if s not in exceptions]
    action: SourceSpan | None = None
    condition: SourceSpan | None = None
    exception: SourceSpan | None = None
    status = "UNRESOLVED"

    if atom.role != "REQUIREMENT_CANDIDATE" or not normative:
        reasons.add("NOT_VERIFIED_NORMATIVE_REQUIREMENT")
    else:
        cue_start = min(normative)
        if len(normative) > 1:
            reasons.add("MULTIPLE_NORMATIVE_CUES")
        # Prefix delimiter is a defensible source boundary, not a claim that
        # the precondition is logically necessary/sufficient.
        first_delim = next((i for i, ch in enumerate(atom.text[:cue_start]) if ch in _PREFIX_END), -1)
        if len(conditions) + len(exceptions) == 0:
            action = _span(atom, 0, len(atom.text), "ACTION_CANDIDATE")
            status = "ACTION_ONLY"
        elif first_delim >= 0 and (any(i < first_delim for i in conditions + exceptions)):
            # Multiple condition/exception signals in the same prefix cannot
            # safely establish nested or exception precedence.
            if len(conditions) + len(exceptions) == 1:
                if exceptions:
                    exception = _span(atom, 0, first_delim + 1, "EXCEPTION_PREFIX")
                else:
                    condition = _span(atom, 0, first_delim + 1, "CONDITION_PREFIX")
                action = _span(atom, first_delim + 1, len(atom.text), "ACTION_CANDIDATE")
                status = "EXPLICIT_PREFIX_CANDIDATE" if action is not None else "UNRESOLVED"
            else:
                reasons.add("NESTED_CONDITION_OR_EXCEPTION_UNRESOLVED")
        else:
            reasons.add("SCOPE_BOUNDARY_NOT_EXPLICIT")
        # Postposed exceptions/conditions and multi-action clauses cannot be
        # safely assigned to one prefix action, even if a comma was found.
        if status == "EXPLICIT_PREFIX_CANDIDATE" and (
            any(i > first_delim for i in conditions + exceptions)
            or len(normative) != 1
        ):
            reasons.add("COMPOUND_OR_POSTPOSED_SCOPE_UNRESOLVED")
            condition = exception = action = None
            status = "UNRESOLVED"
        if status == "ACTION_ONLY" and len(normative) > 1:
            reasons.add("MULTIPLE_ACTION_CLAUSES_UNRESOLVED")
            action = None
            status = "UNRESOLVED"
        if "CONDITION_OR_EXCEPTION_SCOPE_UNRESOLVED" in atom.review_reasons:
            reasons.add("PARENT_ATOMIC_SCOPE_UNRESOLVED")
            # There is no proof that the condition for one atomic sentence
            # does or does not apply to the next sentence in the same parent.
            condition = exception = action = None
            status = "UNRESOLVED"
    if status == "UNRESOLVED" and not reasons:
        reasons.add("ENGINEERING_SCOPE_REVIEW_REQUIRED")
    evidence = RequirementScopeEvidence(
        candidate_key=atom.candidate_key, parent_unit_key=atom.parent_unit_key,
        modality=signals.modality, status=status,
        condition=condition, exception=exception, action=action,
        review_reasons=tuple(sorted(reasons)),
    )
    validate_requirement_scope(unit, atom, evidence)
    return evidence


def validate_requirement_scope(
    unit: RequirementUnit, atom: AtomicCandidate, evidence: RequirementScopeEvidence,
) -> None:
    if evidence.candidate_key != atom.candidate_key or evidence.parent_unit_key != unit.unit_key:
        raise ValueError("Scope source identity mismatch")
    for field in (evidence.condition, evidence.exception, evidence.action):
        if field is None:
            continue
        if not (atom.start_in_content <= field.start_in_content < field.end_in_content <= atom.end_in_content):
            raise ValueError("Scope coordinates outside source atom")
        l = field.start_in_content - unit.start_offset
        r = field.end_in_content - unit.start_offset
        if unit.text[l:r] != field.exact_text:
            raise ValueError("Scope span text does not match original source")
    if evidence.condition and evidence.exception:
        raise ValueError("Conflicting conditional and exception prefix detected")
    if evidence.condition and evidence.action and evidence.condition.end_in_content > evidence.action.start_in_content:
        raise ValueError("Overlapping condition/action spans")
    if evidence.exception and evidence.action and evidence.exception.end_in_content > evidence.action.start_in_content:
        raise ValueError("Overlapping exception/action spans")
