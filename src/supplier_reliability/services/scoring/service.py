"""Scoring service (Task 12): persistence, idempotency, versioning, restatements, lineage.

Identity (D8): `evaluation_key = sha256("|".join([supplier_id, site_id or "*", S, E, C,
scoring_version, config_hash]))[:32]`, S/E/C as ISO-8601 UTC with a trailing "Z". Scores are
supplier-level (site_id NULL, "*").

`config_hash` = `scoring_config_hash(cfg)`: sha256 of the canonical JSON (sorted keys,
separators (",", ":")) of the ScoringConfig alone. The ScoringConfigSnapshot stores exactly that
JSON, so a snapshot is verifiable against its hash and generator/anomaly settings never change a
score's identity.

Transactions: the service OWNS commit/rollback. Every public call is ONE transaction (commit on
success, rollback on error) holding the snapshot upsert (first: supplier_scores.config_hash is a
FK, R8), the score rows and their score_evidence rows. Because it commits, a call refuses to
start (RuntimeError) while the session holds pending new/dirty/deleted objects, so it never
commits a caller's unrelated half-done work.
- calculate: a current row for the key -> `existing`; otherwise score and insert version 1.
  calculate with a later cutoff for an already scored window is simply another evaluation (new
  key) and is NOT linked to the earlier one; use `restate` to link and report differences.
- recalculate: recompute; fingerprint equal to the current row's -> `unchanged` (no write);
  otherwise supersede in the schema-enforced order: demote old (is_current=False) + flush,
  insert version+1 + flush, set old.superseded_by_score_id + flush. No current row -> version 1.
  If the current row is a restatement, the new version keeps its restates_score_id and its
  differences_from_restated are recomputed against that same restated score.
- restate: the same window with a later cutoff C' is a new evaluation (new key). The new row's
  restates_score_id is the current score of the same supplier/S/E/scoring_version/config with
  the greatest cutoff < C', and explanation.differences_from_restated holds per-component and
  overall {"from", "to", "delta"} plus, per component, the included lineage ids added/removed:
  "evidence": {component: {"added": [...], "removed": [...]}}.
- score_history: `calculate` for every supplier per window with calculated_at = cutoff + 1 day
  (the config snapshot, if created, gets the first window's calculated_at too).
- stage_corrected (Task 15) is the one exception to "owns the transaction": it writes a
  CORRECTED score (leakage correction) inside the caller's transaction (detect_raw_batch), so
  the correction and its data exception commit together. It flushes and never commits.

Score ids SCORE-2026-NNNNNN come from max(existing) + 1, read once inside the transaction. That is
safe on SQLite (single writer). Concurrent writers on PostgreSQL would need a sequence or a retry
on the primary-key conflict (UNVERIFIED: PostgreSQL is not available on the build machine).

Lineage rows (D8): per component one included row per `source_record_ids` entry and one excluded
row per `excluded_record_ids` entry and per bundle-level ExcludedRecord. The primary key is
(score_id, component, source_entity_type, source_record_id), so repeats of one record in one
component are merged: reasons sorted and "; "-joined, per-month exclusions listed in
contribution["months"]. `contribution` holds the values the component used for that record (the
fingerprint observation; a record observed several times, e.g. a capacity declaration covering
several months, gets {"observations": [...]}). Component-evaluated ids map to entity types by id
prefix; an unknown prefix raises.

`rebuild_from_lineage` reloads exactly the records named by a score's lineage (included rows plus
the rows a component itself evaluated and set aside, i.e. component-level exclusion reasons, plus
each requirement's certification from its contribution) through `collect_evidence(...,
only_record_ids=...)`, so every as-of-C derivation is the same code, and scores them with the
ScoringConfig from the snapshot. The open CRITICAL exceptions are taken from the stored
explanation ("exceptions"), as they are not record lineage (R17: they are processing-time flags).
Delivery lineage includes the ARRIVED/CORRECTION events that determined each arrival, and the
rebuild loads only those events, so a back-dated event inserted later cannot change it.
Known limitation (deferred): a risk signal's site_active is re-derived from the site row as it is
now (site status/effective dates are master data without history), not from lineage.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from supplier_reliability import __version__
from supplier_reliability.config import ScoringConfig
from supplier_reliability.domain.enums import (
    CalculationStatus,
    ComponentName,
    ReliabilityCategory,
)
from supplier_reliability.domain.numeric import ensure_utc
from supplier_reliability.models import ScoreEvidence, ScoringConfigSnapshot, SupplierScore
from supplier_reliability.services.scoring import evidence_queries as q
from supplier_reliability.services.scoring.components import COMPONENT_EXCLUSION_REASONS
from supplier_reliability.services.scoring.engine import (
    ScoreResult,
    evidence_observations,
    score_supplier,
)
from supplier_reliability.services.scoring.evidence import (
    RESTRICTABLE_ENTITIES,
    EvaluationWindow,
    EvidenceBundle,
    collect_evidence,
)

SCORE_ID_PREFIX = "SCORE-2026-"
_SCORE_ID_MAX = 999_999
_ENTITY_BY_PREFIX = (
    ("SHP-", "shipment"),
    ("EVT-", "delivery_event"),
    ("INS-", "inspection"),
    ("INV-", "invoice"),
    ("POL-", "po_line"),
    ("CAP-", "capacity_declaration"),
    ("REQ-", "compliance_requirement"),
    ("CERT-", "supplier_certification"),
    ("RSK-", "risk_signal"),
)
_COMPONENTS = [c.value for c in ComponentName]


# --- identity -------------------------------------------------------------------------------


def entity_type_for(record_id: str) -> str:
    """source_entity_type of a component-evaluated record id, by its prefix."""
    for prefix, entity in _ENTITY_BY_PREFIX:
        if record_id.startswith(prefix):
            return entity
    raise ValueError(f"cannot map record id {record_id!r} to a source entity type")


def scoring_config_hash(cfg: ScoringConfig) -> str:
    return hashlib.sha256(_canonical(cfg.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iso(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def evaluation_key(
    supplier_id: str,
    site_id: str | None,
    window: EvaluationWindow,
    scoring_version: str,
    config_hash: str,
) -> str:
    parts = [
        supplier_id,
        site_id or "*",
        _iso(window.start),
        _iso(window.end),
        _iso(window.cutoff),
        scoring_version,
        config_hash,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# --- requests / outcomes --------------------------------------------------------------------


@dataclass(frozen=True)
class CalculationRequest:
    window: EvaluationWindow
    supplier_ids: list[str] | None = None  # None: every supplier
    scoring_version: str | None = None  # None: the configured one; any other raises


@dataclass
class CalculationOutcome:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # old score_id -> new score_id


class NothingToRestateError(ValueError):
    """`restate` found no earlier score to restate (the only ValueError that is a request
    conflict rather than a fault). `missing` holds the suppliers without one (empty when there
    are no suppliers at all); `cutoff` is the requested cutoff as ISO-8601 UTC."""

    def __init__(self, cutoff: str, missing: list[str]) -> None:
        self.cutoff, self.missing = cutoff, missing
        super().__init__(
            f"no earlier score with a cutoff before {cutoff} to restate for "
            f"{missing or 'any supplier'}"
        )


class CorrectionOutcome(StrEnum):
    """What `stage_corrected` did (stored in the leakage exception's evidence)."""

    EXISTING = "eligible canonical score already exists"
    INSERTED = "corrected score inserted"
    SUPERSEDED = "stale current score superseded"


@dataclass(frozen=True)
class _NewScore:
    supplier_id: str
    key: str
    bundle: EvidenceBundle
    result: ScoreResult
    version: int = 1
    change_reason: str | None = None
    restates: SupplierScore | None = None
    status: CalculationStatus | None = None  # None: derived from the result


def calculation_status(result: ScoreResult) -> CalculationStatus:
    if result.overall.category == ReliabilityCategory.INSUFFICIENT_EVIDENCE:
        return CalculationStatus.INSUFFICIENT_EVIDENCE
    if result.overall.is_provisional:
        return CalculationStatus.PROVISIONAL
    return CalculationStatus.CALCULATED


# --- service --------------------------------------------------------------------------------


class ScoringService:
    def __init__(self, session: Session, cfg: ScoringConfig, clock: Callable[[], datetime]) -> None:
        self.session = session
        self.cfg = cfg
        self.clock = clock
        self.config_hash = scoring_config_hash(cfg)
        self._next_number: int | None = None

    # -- public API ----------------------------------------------------------------------------

    def calculate(self, req: CalculationRequest) -> CalculationOutcome:
        self._check_version(req.scoring_version)
        return self._transaction(
            lambda out: self._calculate(req.window, req.supplier_ids, self.clock(), out)
        )

    def recalculate(self, req: CalculationRequest, reason: str) -> CalculationOutcome:
        self._check_version(req.scoring_version)
        return self._transaction(lambda out: self._recalculate(req, reason, out))

    def restate(
        self, window: EvaluationWindow, supplier_ids: list[str] | None, reason: str
    ) -> CalculationOutcome:
        return self._transaction(lambda out: self._restate(window, supplier_ids, reason, out))

    def score_history(self, windows: list[EvaluationWindow]) -> CalculationOutcome:
        """Pipeline helper: every supplier, every window, calculated_at = cutoff + 1 day."""

        def work(out: CalculationOutcome) -> None:
            for w in windows:
                self._calculate(w, None, w.cutoff + timedelta(days=1), out)

        return self._transaction(work)

    def stage_corrected(
        self, window: EvaluationWindow, supplier_id: str, reason: str
    ) -> tuple[str, CorrectionOutcome]:
        """The eligible-evidence score for (supplier, window) as a leakage correction (Task 15,
        Ruling R4), inside the CALLER's transaction: it flushes and never commits (the caller,
        `detect_raw_batch`, commits or rolls back). Returns (score_id, outcome):
        - EXISTING: the current score for the evaluation key already has the fresh evidence
          fingerprint; nothing is written.
        - INSERTED: no current score for the key; version 1 is inserted with
          calculation_status CORRECTED.
        - SUPERSEDED: the current score's fingerprint differs; version + 1 (CORRECTED) supersedes
          it in the schema-enforced order, as `recalculate` does.
        Scores, lineage and snapshot are written by the same `_insert` as every other score."""
        self._next_number = None
        ids = self._supplier_ids([supplier_id])
        key = self._keys(ids, window)[supplier_id]
        old = self._current([key]).get(key)
        bundle, result = self._score(ids, window)[supplier_id]
        if old is not None and old.evidence_fingerprint == result.evidence_fingerprint:
            return old.score_id, CorrectionOutcome.EXISTING
        corrected = CalculationStatus.CORRECTED
        if old is None:
            new = _NewScore(supplier_id, key, bundle, result, 1, reason, status=corrected)
            return self._insert([new], self.clock())[0], CorrectionOutcome.INSERTED
        restated = self._by_id([old.restates_score_id] if old.restates_score_id else [])
        new = _NewScore(
            supplier_id,
            key,
            bundle,
            result,
            old.version_number + 1,
            reason,
            restated.get(old.restates_score_id or ""),
            corrected,
        )
        superseded, _ = self._supersede([(old, new)], [])
        return superseded[old.score_id], CorrectionOutcome.SUPERSEDED

    # -- transaction plumbing ------------------------------------------------------------------

    def _check_version(self, requested: str | None) -> None:
        if requested is not None and requested != self.cfg.scoring_version:
            raise ValueError(
                f"scoring version {requested!r} not available "
                f"(configured: {self.cfg.scoring_version!r})"
            )

    def _transaction(self, work: Callable[[CalculationOutcome], None]) -> CalculationOutcome:
        s = self.session
        if s.new or s.dirty or s.deleted:
            raise RuntimeError(
                "session has pending changes; commit or roll them back before calling the "
                "scoring service (it commits its own transaction)"
            )
        out = CalculationOutcome()
        self._next_number = None
        try:
            work(out)
            self.session.commit()
        except BaseException:
            self.session.rollback()
            raise
        finally:
            self._next_number = None
        return out

    def _allocate_ids(self, n: int) -> list[str]:
        if self._next_number is None:
            top = self.session.scalar(
                sa.select(sa.func.max(SupplierScore.score_id)).where(
                    SupplierScore.score_id.like(f"{SCORE_ID_PREFIX}%")
                )
            )
            self._next_number = 1 if top is None else int(top.removeprefix(SCORE_ID_PREFIX)) + 1
        first = self._next_number
        if first + n - 1 > _SCORE_ID_MAX:
            raise ValueError("score id space SCORE-2026-NNNNNN exhausted")
        self._next_number = first + n
        return [f"{SCORE_ID_PREFIX}{i:06d}" for i in range(first, first + n)]

    def _ensure_snapshot(self, created_at: datetime) -> None:
        if self.session.get(ScoringConfigSnapshot, self.config_hash) is None:
            self.session.add(
                ScoringConfigSnapshot(
                    config_hash=self.config_hash,
                    scoring_version=self.cfg.scoring_version,
                    config=self.cfg.model_dump(mode="json"),
                    created_at=ensure_utc(created_at),
                )
            )
            self.session.flush()

    # -- operations ----------------------------------------------------------------------------

    def _supplier_ids(self, supplier_ids: Sequence[str] | None) -> list[str]:
        wanted = None if supplier_ids is None else sorted(set(supplier_ids))
        known = q.existing_supplier_ids(self.session, wanted)
        if wanted is not None and len(known) != len(wanted):
            raise ValueError(f"unknown supplier ids: {sorted(set(wanted) - set(known))}")
        return known

    def _keys(self, ids: Iterable[str], window: EvaluationWindow) -> dict[str, str]:
        version, h = self.cfg.scoring_version, self.config_hash
        return {sid: evaluation_key(sid, None, window, version, h) for sid in ids}

    def _current(self, keys: Iterable[str]) -> dict[str, SupplierScore]:
        stmt = sa.select(SupplierScore).where(
            SupplierScore.evaluation_key.in_(sorted(set(keys))),
            SupplierScore.is_current.is_(True),
        )
        return {row.evaluation_key: row for row in self.session.scalars(stmt)}

    def _score(
        self, ids: list[str], window: EvaluationWindow
    ) -> dict[str, tuple[EvidenceBundle, ScoreResult]]:
        if not ids:
            return {}
        bundles = collect_evidence(self.session, ids, window, lookback_days=self.cfg.lookback_days)
        return {
            sid: (b, score_supplier(b, self.cfg, self.config_hash)) for sid, b in bundles.items()
        }

    def _calculate(
        self,
        window: EvaluationWindow,
        supplier_ids: Sequence[str] | None,
        calculated_at: datetime,
        out: CalculationOutcome,
    ) -> None:
        ids = self._supplier_ids(supplier_ids)
        keys = self._keys(ids, window)
        current = self._current(keys.values())
        out.existing.extend(current[keys[sid]].score_id for sid in ids if keys[sid] in current)
        todo = [sid for sid in ids if keys[sid] not in current]
        scored = self._score(todo, window)
        new = [_NewScore(sid, keys[sid], *scored[sid]) for sid in todo]
        out.created.extend(self._insert(new, calculated_at))

    def _recalculate(self, req: CalculationRequest, reason: str, out: CalculationOutcome) -> None:
        ids = self._supplier_ids(req.supplier_ids)
        keys = self._keys(ids, req.window)
        current = self._current(keys.values())
        scored = self._score(ids, req.window)
        restated = self._by_id(
            [row.restates_score_id for row in current.values() if row.restates_score_id]
        )
        fresh: list[_NewScore] = []
        changed: list[tuple[SupplierScore, _NewScore]] = []
        for sid in ids:
            bundle, result = scored[sid]
            old = current.get(keys[sid])
            if old is None:
                fresh.append(_NewScore(sid, keys[sid], bundle, result))
            elif old.evidence_fingerprint == result.evidence_fingerprint:
                out.unchanged.append(old.score_id)
            else:
                new = _NewScore(
                    sid,
                    keys[sid],
                    bundle,
                    result,
                    old.version_number + 1,
                    reason,
                    restated.get(old.restates_score_id or ""),
                )
                changed.append((old, new))
        superseded, created = self._supersede(changed, fresh)
        out.superseded.update(superseded)
        out.created.extend(created)

    def _supersede(
        self, changed: list[tuple[SupplierScore, _NewScore]], fresh: list[_NewScore]
    ) -> tuple[dict[str, str], list[str]]:
        """Insert each `changed` successor over its old row, plus the `fresh` version-1 rows, in
        the schema-enforced supersession order: demote the old rows (is_current=False) + flush,
        insert the new rows + flush, link old.superseded_by_score_id + flush. Returns
        ({old score_id: new score_id}, ids of the fresh rows). The one place this order lives
        (used by recalculate and stage_corrected)."""
        for old, _ in changed:
            old.is_current = False
        self.session.flush()
        new_ids = self._insert([new for _, new in changed] + fresh, self.clock())
        superseded: dict[str, str] = {}
        for (old, _), new_id in zip(changed, new_ids, strict=False):
            old.superseded_by_score_id = new_id
            superseded[old.score_id] = new_id
        self.session.flush()
        return superseded, new_ids[len(changed) :]

    def _by_id(self, score_ids: list[str]) -> dict[str, SupplierScore]:
        if not score_ids:
            return {}
        stmt = sa.select(SupplierScore).where(SupplierScore.score_id.in_(sorted(set(score_ids))))
        return {row.score_id: row for row in self.session.scalars(stmt)}

    def _restate(
        self,
        window: EvaluationWindow,
        supplier_ids: list[str] | None,
        reason: str,
        out: CalculationOutcome,
    ) -> None:
        ids = None if supplier_ids is None else self._supplier_ids(supplier_ids)
        s = SupplierScore
        stmt = sa.select(s).where(
            s.is_current.is_(True),
            s.site_id.is_(None),
            s.evaluation_start == window.start,
            s.evaluation_end == window.end,
            s.scoring_version == self.cfg.scoring_version,
            s.config_hash == self.config_hash,
            s.data_cutoff_timestamp < window.cutoff,
        )
        if ids is not None:
            stmt = stmt.where(s.supplier_id.in_(ids))
        restated: dict[str, SupplierScore] = {}
        for row in self.session.scalars(stmt):
            best = restated.get(row.supplier_id)
            if best is None or row.data_cutoff_timestamp > best.data_cutoff_timestamp:
                restated[row.supplier_id] = row
        targets = sorted(restated) if ids is None else ids
        missing = [sid for sid in targets if sid not in restated]
        if not targets or missing:
            raise NothingToRestateError(_iso(window.cutoff), missing)
        keys = self._keys(targets, window)
        current = self._current(keys.values())
        out.existing.extend(current[keys[sid]].score_id for sid in targets if keys[sid] in current)
        todo = [sid for sid in targets if keys[sid] not in current]
        scored = self._score(todo, window)
        new = [_NewScore(sid, keys[sid], *scored[sid], 1, reason, restated[sid]) for sid in todo]
        out.created.extend(self._insert(new, self.clock()))

    # -- writing -------------------------------------------------------------------------------

    def _insert(self, new: list[_NewScore], calculated_at: datetime) -> list[str]:
        """Bulk-insert the scores and their lineage; returns the new ids in `new` order."""
        if not new:
            return []
        self._ensure_snapshot(calculated_at)
        ids = self._allocate_ids(len(new))
        old_included = self._included_ids([n.restates.score_id for n in new if n.restates])
        score_rows, evidence_rows = [], []
        for score_id, n in zip(ids, new, strict=True):
            explanation = n.result.explanation
            if n.restates is not None:
                explanation = explanation | {
                    "differences_from_restated": _differences(
                        n.restates, n.result, old_included.get(n.restates.score_id, {})
                    )
                }
            score_rows.append(self._score_row(score_id, n, explanation, calculated_at))
            evidence_rows.extend(_evidence_rows(score_id, n.bundle, n.result))
        self.session.execute(sa.insert(SupplierScore), score_rows)
        if evidence_rows:
            self.session.execute(sa.insert(ScoreEvidence), evidence_rows)
        self.session.flush()
        return ids

    def _included_ids(self, score_ids: list[str]) -> dict[str, dict[str, set[str]]]:
        """score_id -> component -> included source record ids."""
        if not score_ids:
            return {}
        stmt = sa.select(
            ScoreEvidence.score_id, ScoreEvidence.component, ScoreEvidence.source_record_id
        ).where(ScoreEvidence.score_id.in_(score_ids), ScoreEvidence.included.is_(True))
        out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for score_id, component, record_id in self.session.execute(stmt):
            out[score_id][ComponentName(component).value].add(record_id)
        return out

    def _score_row(
        self,
        score_id: str,
        n: _NewScore,
        explanation: dict[str, Any],
        calculated_at: datetime,
    ) -> dict[str, Any]:
        r, w = n.result, n.result.window
        row: dict[str, Any] = {
            "score_id": score_id,
            "supplier_id": n.supplier_id,
            "site_id": None,
            "evaluation_start": w.start,
            "evaluation_end": w.end,
            "data_cutoff_timestamp": w.cutoff,
            "calculated_at": ensure_utc(calculated_at),
            "overall_score": r.overall.overall,
            "evidence_coverage": r.overall.evidence_coverage,
            "is_provisional": r.overall.is_provisional,
            "reliability_category": r.overall.category,
            "decision": r.overall.decision,
            "calculation_status": n.status or calculation_status(r),
            "effective_weights": {k: str(v) for k, v in r.overall.effective_weights.items()},
            "explanation": explanation,
            "scoring_version": self.cfg.scoring_version,
            "config_hash": self.config_hash,
            "code_version": __version__,
            "evaluation_key": n.key,
            "version_number": n.version,
            "is_current": True,
            "superseded_by_score_id": None,
            "change_reason": n.change_reason,
            "evidence_fingerprint": r.evidence_fingerprint,
            "restates_score_id": None if n.restates is None else n.restates.score_id,
        }
        for name in _COMPONENTS:
            row[f"{name}_score"] = r.components[name].score
        return row


# --- lineage --------------------------------------------------------------------------------


def _evidence_rows(score_id: str, bundle: EvidenceBundle, result: ScoreResult) -> list[dict]:
    values: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for o in evidence_observations(bundle):
        rest = {k: v for k, v in o.items() if k not in ("component", "record_id")}
        values[(o["component"], o["record_id"])].append(rest)

    # (component, entity, record id) -> merged state
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    def entry(component: str, entity: str, record_id: str) -> dict[str, Any]:
        key = (component, entity, record_id)
        if key not in merged:
            merged[key] = {"included": False, "reasons": set(), "months": set(), "values": []}
        return merged[key]

    for name, comp in result.components.items():
        for record_id in comp.source_record_ids:
            e = entry(name, entity_type_for(record_id), record_id)
            e["included"], e["values"] = True, values[(name, record_id)]
        for record_id, reason in comp.excluded_record_ids:
            e = entry(name, entity_type_for(record_id), record_id)
            e["reasons"].add(reason)
            e["values"] = e["values"] or values.get((name, record_id), [])
    for x in bundle.excluded:
        e = entry(x.component.value, x.source_entity_type, x.source_record_id)
        e["reasons"].add(x.reason)
        if x.month is not None:
            e["months"].add(x.month.isoformat())

    rows = []
    for (component, entity, record_id), e in sorted(merged.items()):
        contribution: dict[str, Any] = {}
        if len(e["values"]) == 1:
            contribution.update(e["values"][0])
        elif e["values"]:
            contribution["observations"] = e["values"]
        if e["months"]:
            contribution["months"] = sorted(e["months"])
        if e["included"] and e["reasons"]:  # used for some months, set aside for others
            contribution["partial_exclusions"] = sorted(e["reasons"])
        rows.append(
            {
                "score_id": score_id,
                "component": ComponentName(component),
                "source_entity_type": entity,
                "source_record_id": record_id,
                "included": e["included"],
                "exclusion_reason": None if e["included"] else "; ".join(sorted(e["reasons"])),
                "contribution": contribution,
            }
        )
    return rows


def _delta(old: Decimal | None, new: Decimal | None) -> dict[str, str | None]:
    return {
        "from": None if old is None else str(old),
        "to": None if new is None else str(new),
        "delta": None if old is None or new is None else str(new - old),
    }


def _differences(
    old: SupplierScore, new: ScoreResult, old_ids: dict[str, set[str]]
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "restated_score_id": old.score_id,
        "restated_data_cutoff": _iso(old.data_cutoff_timestamp),
    }
    for name in _COMPONENTS:
        out[name] = _delta(getattr(old, f"{name}_score"), new.components[name].score)
    out["overall"] = _delta(old.overall_score, new.overall.overall)
    evidence: dict[str, dict[str, list[str]]] = {}
    for name in _COMPONENTS:
        before = old_ids.get(name, set())
        after = set(new.components[name].source_record_ids)
        evidence[name] = {"added": sorted(after - before), "removed": sorted(before - after)}
    out["evidence"] = evidence
    return out


def rebuild_from_lineage(session: Session, score_id: str) -> ScoreResult:
    """Recompute a stored score from its score_evidence rows and ScoringConfigSnapshot only
    (see the module docstring). Raises LookupError for an unknown score or missing snapshot."""
    score = session.get(SupplierScore, score_id)
    if score is None:
        raise LookupError(f"score {score_id} not found")
    snapshot = session.get(ScoringConfigSnapshot, score.config_hash)
    if snapshot is None:
        raise LookupError(f"config snapshot {score.config_hash} not found")
    cfg = ScoringConfig.model_validate(snapshot.config)
    if scoring_config_hash(cfg) != score.config_hash:
        raise ValueError(f"config snapshot {score.config_hash} does not match its hash")

    only: dict[str, set[str]] = defaultdict(set)
    rows = session.scalars(sa.select(ScoreEvidence).where(ScoreEvidence.score_id == score_id))
    for e in rows:
        reasons = set((e.exclusion_reason or "").split("; "))
        evaluated = e.included or bool(reasons & COMPONENT_EXCLUSION_REASONS)
        if not evaluated or e.source_entity_type not in RESTRICTABLE_ENTITIES:
            continue
        only[e.source_entity_type].add(e.source_record_id)
        certification_id = e.contribution.get("certification_id")
        if e.component == ComponentName.compliance and certification_id:
            only["supplier_certification"].add(certification_id)

    window = EvaluationWindow(
        score.evaluation_start, score.evaluation_end, score.data_cutoff_timestamp
    )
    bundle = collect_evidence(
        session,
        [score.supplier_id],
        window,
        lookback_days=cfg.lookback_days,
        only_record_ids=only,
    )[score.supplier_id]
    bundle = replace(bundle, open_critical_exception_ids=list(score.explanation["exceptions"]))
    return score_supplier(bundle, cfg, score.config_hash)
