"""Deterministic scoring benchmarks (Task 12b, Add-D).

Real source records are built from QuarterSpecs into a migrated temp DB and scored through
ScoringService. Every expected value comes from expected.yaml, hand-calculated from D6/D7 with
the arithmetic written next to it; nothing is read back from the engine to form an expectation.
"""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from supplier_reliability.config import ScoringConfig
from supplier_reliability.database import make_engine, make_session_factory
from supplier_reliability.domain.enums import (
    CertificationStatus,
    CertificationType,
    ComponentName,
    Decision,
    DeliveryEventType,
    InspectionKind,
    ReliabilityCategory,
    Severity,
)
from supplier_reliability.models import (
    Contract,
    DeliveryEvent,
    ScoreEvidence,
    ShipmentLineAllocation,
    Site,
    SupplierCertification,
    SupplierScore,
)
from supplier_reliability.services.scoring.service import CalculationRequest, ScoringService

from ..factories import GraphBuilder, utc
from .builders import QuarterSpec, build_supplier_history
from .scenarios import ONE_FACTOR, QUARTERS, SCENARIOS, STABLE_Q, SUPPLIER, WINDOWS

EXPECTED = yaml.safe_load((Path(__file__).parent / "expected.yaml").read_text(encoding="utf-8"))
COMPONENTS = [c.value for c in ComponentName]
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
# category rank, best first (for "the category drops")
RANK = [
    ReliabilityCategory.HIGH,
    ReliabilityCategory.ACCEPTABLE,
    ReliabilityCategory.AT_RISK,
    ReliabilityCategory.CRITICAL,
]


def dec(value: float | int | str) -> Decimal:
    return Decimal(str(value))


@pytest.fixture
def cfg(app_config) -> ScoringConfig:
    return app_config.scoring


@contextmanager
def open_db(template: Path, path: Path) -> Iterator[Session]:
    shutil.copyfile(template, path)
    engine = make_engine(f"sqlite:///{path}")
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


def score_quarters(session: Session, cfg: ScoringConfig, count: int) -> list[SupplierScore]:
    """Score the supplier for the first `count` 2025 quarter windows; the persisted rows."""
    svc = ScoringService(session, cfg, lambda: NOW)
    rows = []
    for window in WINDOWS[:count]:
        (score_id,) = svc.calculate(CalculationRequest(window, [SUPPLIER])).created
        rows.append(session.get(SupplierScore, score_id))
    return rows


def build_and_score(session: Session, cfg: ScoringConfig, specs: list[QuarterSpec]):
    build_supplier_history(session, SUPPLIER, specs)
    session.commit()
    return score_quarters(session, cfg, len(specs))


def components(row: SupplierScore) -> dict[str, Decimal | None]:
    return {c: getattr(row, f"{c}_score") for c in COMPONENTS}


# --- hand-calculated benchmarks --------------------------------------------------------------


def test_expected_overall_reconciles_with_hand_components(cfg):
    """Guards the hand arithmetic itself: every expected overall is round2(sum w x c)."""
    weights = {k: dec(v) for k, v in cfg.weights.items()}
    for quarters in EXPECTED["scenarios"].values():
        for q in quarters:
            total = sum(weights[c] * dec(q[c]) for c in COMPONENTS)
            assert total.quantize(Decimal("0.01"), "ROUND_HALF_UP") == dec(q["overall"]), q


@pytest.mark.parametrize("scenario", ["stable", "deteriorating", "recovering"])
def test_benchmark_scores_match_hand_calculation(db_session, cfg, scenario):
    rows = build_and_score(db_session, cfg, SCENARIOS[scenario])
    expected = EXPECTED["scenarios"][scenario]
    assert [q["quarter"] for q in expected] == QUARTERS
    for row, want in zip(rows, expected, strict=True):
        label = f"{scenario} {want['quarter']}"
        assert components(row) == {c: dec(want[c]) for c in COMPONENTS}, label
        assert row.overall_score == dec(want["overall"]), label
        assert row.reliability_category == ReliabilityCategory(want["category"]), label
        assert row.decision == Decision(want["decision"]), label


def test_deteriorating_supplier_drops_and_is_suspended(db_session, cfg):
    q1, q2, q3, q4 = build_and_score(db_session, cfg, SCENARIOS["deteriorating"])
    assert RANK.index(q4.reliability_category) > RANK.index(q3.reliability_category)
    assert q4.decision == Decision.SUSPEND_PENDING_REVIEW
    # D7 "deteriorating" condition (the report itself is Task 17): Q4 >= 10 points below Q3,
    # neither insufficient
    assert ReliabilityCategory.INSUFFICIENT_EVIDENCE not in (
        q3.reliability_category,
        q4.reliability_category,
    )
    assert q3.overall_score - q4.overall_score >= dec(cfg.deteriorating_threshold)


def test_recovering_supplier_improves_every_quarter(db_session, cfg):
    rows = build_and_score(db_session, cfg, SCENARIOS["recovering"])
    overall = [r.overall_score for r in rows]
    assert overall == sorted(set(overall))  # strictly increasing
    assert RANK.index(rows[-1].reliability_category) <= RANK.index(ReliabilityCategory.ACCEPTABLE)


@pytest.mark.parametrize("factor", COMPONENTS)
def test_one_factor_changes_only_its_component(db_session, cfg, factor):
    (row,) = build_and_score(db_session, cfg, [ONE_FACTOR[factor]])
    baseline = EXPECTED["scenarios"]["stable"][0]  # hand-calculated stable Q1
    want = EXPECTED["one_factor"][factor]
    got = components(row)
    for c in COMPONENTS:
        if c == factor:
            assert got[c] == dec(want["score"])
            assert got[c] - dec(baseline[c]) == dec(want["delta"])
        else:
            assert got[c] == dec(baseline[c]), f"{c} changed when perturbing {factor}"
    weight = dec(cfg.weights[factor])
    assert row.overall_score - dec(baseline["overall"]) == weight * dec(want["delta"])
    assert row.overall_score == dec(want["overall"])


# --- determinism and eligibility ---------------------------------------------------------------


def test_identical_evidence_and_config_identical_results(migrated_template, tmp_path, cfg):
    results = []
    for name in ("a.db", "b.db"):
        with open_db(migrated_template, tmp_path / name) as session:
            rows = build_and_score(session, cfg, SCENARIOS["deteriorating"])
            results.append(
                [
                    (
                        components(r),
                        r.overall_score,
                        r.reliability_category,
                        r.decision,
                        r.evidence_fingerprint,
                    )
                    for r in rows
                ]
            )
    assert results[0] == results[1]
    assert len({fp for *_, fp in results[0]}) == 4  # one distinct fingerprint per quarter


def _add_outside_window_records(session: Session, graph: GraphBuilder) -> None:
    """Adversarial records for the Q1 2025 window [2025-01-01, 2025-03-31T23:59:59], C = E:
    each would lower some component if it were (wrongly) eligible."""
    site_id = session.scalar(sa.select(Site.site_id).where(Site.supplier_id == SUPPLIER))
    contract_id = session.scalar(
        sa.select(Contract.contract_id).where(Contract.supplier_id == SUPPLIER)
    )
    cutoff = WINDOWS[0].cutoff
    after_c = cutoff + timedelta(days=2)

    def bad_shipment(order: date, expected: datetime, created_at: datetime | None = None):
        po = graph.purchase_order(
            supplier_id=SUPPLIER, contract_id=contract_id, order_date=order, created_at=created_at
        )
        line = graph.po_line(po_id=po, ordered_quantity="50000", unit_of_measure="units/month")
        shp = graph.shipment(po_id=po, site_id=site_id, expected=expected, created_at=created_at)
        sl = graph.allocation(shipment_id=shp, po_line_id=line, quantity="100")
        graph.event(shipment_id=shp, event_type=DeliveryEventType.ARRIVED,
                    event_timestamp=expected + timedelta(days=15))  # fmt: skip
        late_record = None if created_at is None else created_at + timedelta(days=1)
        graph.inspection(shipment_line_id=sl, inspection_timestamp=expected + timedelta(days=1),
                         recorded_at=late_record, inspected="100", defect="100")  # fmt: skip
        graph.invoice(shipment_id=shp, invoice_date=(expected + timedelta(days=2)).date(),
                      expected="1000.00", invoiced="3000.00", recorded_at=late_record)  # fmt: skip

    # before S
    bad_shipment(date(2024, 12, 2), utc(2024, 12, 10, 12))
    graph.capacity(supplier_id=SUPPLIER, month=date(2024, 12, 1), available="10")
    # before the 180-day lookback horizon (C - 180 d = 2024-10-02); a signal observed between
    # the horizon and S is legitimately eligible for disruption (D5), so it is not used here
    graph.risk_signal(supplier_id=SUPPLIER, observed=utc(2024, 9, 1), severity=Severity.CRITICAL)
    # after E
    bad_shipment(date(2025, 4, 2), utc(2025, 4, 10, 12))
    graph.capacity(supplier_id=SUPPLIER, month=date(2025, 4, 1), available="10")
    graph.risk_signal(supplier_id=SUPPLIER, observed=utc(2025, 4, 5), severity=Severity.CRITICAL)
    # occurred inside the window but recorded after C
    bad_shipment(date(2025, 3, 2), utc(2025, 3, 20, 12), created_at=after_c)
    graph.capacity(
        supplier_id=SUPPLIER, month=date(2025, 3, 1), available="10", recorded_at=after_c
    )
    graph.risk_signal(
        supplier_id=SUPPLIER,
        observed=utc(2025, 3, 20),
        recorded=after_c,
        severity=Severity.CRITICAL,
    )
    current_iso = session.scalar(
        sa.select(SupplierCertification.certification_id).where(
            SupplierCertification.certification_type == CertificationType.ISO9001
        )
    )
    graph.certification(supplier_id=SUPPLIER, compliance=CertificationStatus.NONCOMPLIANT,
                        recorded_at=after_c, supersedes=current_iso)  # fmt: skip
    graph.requirement(contract_id=contract_id, certification_type=CertificationType.REACH,
                      created_at=after_c)  # fmt: skip
    arrived = session.scalars(
        sa.select(DeliveryEvent)
        .where(DeliveryEvent.event_type == DeliveryEventType.ARRIVED)
        .order_by(DeliveryEvent.delivery_event_id)
    ).first()
    graph.correction(shipment_id=arrived.shipment_id, corrects_event_id=arrived.delivery_event_id,
                     corrected_value=arrived.event_timestamp + timedelta(days=20),
                     recorded_timestamp=after_c)  # fmt: skip
    first_line = session.scalar(sa.select(sa.func.min(ShipmentLineAllocation.shipment_line_id)))
    graph.inspection(shipment_line_id=first_line, inspection_timestamp=utc(2025, 3, 30),
                     recorded_at=after_c, inspected="100", defect="100",
                     kind=InspectionKind.REINSPECTION)  # fmt: skip


def test_evidence_outside_window_cannot_affect_score(migrated_template, tmp_path, cfg):
    with open_db(migrated_template, tmp_path / "plain.db") as session:
        (plain,) = build_and_score(session, cfg, [STABLE_Q])
        plain_result = (components(plain), plain.overall_score, plain.evidence_fingerprint)
    with open_db(migrated_template, tmp_path / "noisy.db") as session:
        graph = GraphBuilder(session)
        build_supplier_history(session, SUPPLIER, [STABLE_Q], graph=graph)
        _add_outside_window_records(session, graph)
        session.commit()
        (noisy,) = score_quarters(session, cfg, 1)
        assert (components(noisy), noisy.overall_score, noisy.evidence_fingerprint) == (
            plain_result
        )
        # non-vacuous: the in-window late-recorded records were seen and set aside at C
        reasons = set(
            session.scalars(
                sa.select(ScoreEvidence.exclusion_reason).where(
                    ScoreEvidence.score_id == noisy.score_id, ScoreEvidence.included.is_(False)
                )
            )
        )
    assert {
        "shipment_recorded_after_cutoff",
        "inspection_recorded_after_cutoff",
        "invoice_recorded_after_cutoff",
        "capacity_recorded_after_cutoff",
        "signal_recorded_after_cutoff",
    } <= reasons
