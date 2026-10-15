"""Local-only, auditable engineering intelligence foundations.

No import side effects: database writes are explicit operator actions.
"""

from .requirements import RequirementUnit, RequirementSignals, extract_requirement_units, validate_extracted_units
from .history import UnitMapping, align_requirement_versions
from .relations import RelationCandidate, link_explicit_references
from .atomic import AtomicCandidate, extract_atomic_candidates, validate_atomic_candidates
from .tables import TableBlockEvidence, extract_table_blocks, verify_table_blocks

__all__ = [
    "RequirementUnit", "RequirementSignals", "extract_requirement_units", "validate_extracted_units",
    "UnitMapping", "align_requirement_versions",
    "RelationCandidate", "link_explicit_references",
    "AtomicCandidate", "extract_atomic_candidates", "validate_atomic_candidates",
    "TableBlockEvidence", "extract_table_blocks", "verify_table_blocks",
]
