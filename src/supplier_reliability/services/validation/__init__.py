"""Validation framework (Task 13). Importing the package registers every rule."""

from supplier_reliability.services.validation import (  # noqa: F401  (registration)
    rules_arithmetic,
    rules_chronology,
    rules_domain,
    rules_integrity,
    rules_semantic,
)
from supplier_reliability.services.validation.framework import (
    REGISTRY,
    SEVERITY_RANK,
    Finding,
    Rule,
    RuleCheck,
    ValidationRunResult,
    Violation,
    rule,
    run_validation,
)
from supplier_reliability.services.validation.payloads import (
    PAYLOAD_ENTITY_TYPES,
    validate_payload,
)

__all__ = [
    "PAYLOAD_ENTITY_TYPES",
    "REGISTRY",
    "SEVERITY_RANK",
    "Finding",
    "Rule",
    "RuleCheck",
    "ValidationRunResult",
    "Violation",
    "rule",
    "run_validation",
    "validate_payload",
]
