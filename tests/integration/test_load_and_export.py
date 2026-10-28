"""Task 8: loader (one transaction, refuses reloads) and deterministic export + manifest."""

import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from supplier_reliability.database import make_session_factory, session_scope
from supplier_reliability.models import Base
from supplier_reliability.services.export import (
    DEFAULT_EXPORT_TABLES,
    FileEntry,
    build_manifest,
    execution_metadata,
    export_tables,
)
from supplier_reliability.services.generator.config import GeneratorConfig
from supplier_reliability.services.generator.dataset import generate_dataset
from supplier_reliability.services.generator.loader import (
    LOAD_ORDER,
    DatasetAlreadyLoadedError,
    load_dataset,
)

MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.fixture(scope="module")
def starter(app_config):
    cfg = GeneratorConfig.from_app_config(app_config, "starter")
    return cfg, generate_dataset(cfg)


@pytest.fixture
def loaded(migrated_engine, starter):
    cfg, ds = starter
    factory = make_session_factory(migrated_engine)
    with session_scope(factory) as s:
        counts = load_dataset(s, ds)
    return factory, counts


def _count(session, table):
    return session.scalar(sa.select(sa.func.count()).select_from(Base.metadata.tables[table]))


# --- loader ---------------------------------------------------------------------------------


def test_load_counts_match_dataset(loaded, starter):
    _, ds = starter
    factory, counts = loaded
    assert list(counts) == list(LOAD_ORDER)
    with session_scope(factory) as s:
        for table in LOAD_ORDER:
            assert counts[table] == len(getattr(ds, table)) == _count(s, table), table
    assert counts["inspections"] == 600


def test_load_twice_refused(loaded, starter):
    _, ds = starter
    factory, _ = loaded
    with pytest.raises(DatasetAlreadyLoadedError), session_scope(factory) as s:
        load_dataset(s, ds)
    with session_scope(factory) as s:
        assert _count(s, "suppliers") == 50


def test_load_is_one_transaction(migrated_engine, starter):
    _, ds = starter
    broken = ds.risk_signals[-1] | {"supplier_id": "SUP-999999"}  # FK violation, last table
    bad = type(ds)(**{**ds.__dict__, "risk_signals": [*ds.risk_signals[:-1], broken]})
    factory = make_session_factory(migrated_engine)
    with pytest.raises(sa.exc.IntegrityError), session_scope(factory) as s:
        load_dataset(s, bad)
    with session_scope(factory) as s:
        for table in LOAD_ORDER:
            assert _count(s, table) == 0, table


def test_load_order_is_parent_first():
    order = list(LOAD_ORDER)
    for table in order:
        for fk in Base.metadata.tables[table].foreign_keys:
            parent = fk.column.table.name
            if parent != table:
                assert order.index(parent) < order.index(table), (parent, table)


def test_full_preset_loads_into_migrated_sqlite(app_config, migrated_engine):
    cfg = GeneratorConfig.from_app_config(app_config, "full")
    t0 = time.perf_counter()
    ds = generate_dataset(cfg)
    t1 = time.perf_counter()
    factory = make_session_factory(migrated_engine)
    with session_scope(factory) as s:
        counts = load_dataset(s, ds)
    t2 = time.perf_counter()
    print(f"full preset: generate {t1 - t0:.1f}s, load {t2 - t1:.1f}s")
    assert counts["suppliers"] == 1000
    assert counts["purchase_orders"] == 10000
    assert counts["shipments"] == 15000
    assert counts["inspections"] == 20000


# --- export ---------------------------------------------------------------------------------


def test_default_export_tables_exclude_operational_run_tables():
    names = set(Base.metadata.tables)
    assert set(DEFAULT_EXPORT_TABLES) == names - {
        "dataset_jobs",
        "validation_runs",
        "validation_findings",
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8"), newline="")))


def test_export_uses_lf_and_canonical_formats(loaded, tmp_path):
    factory, _ = loaded
    out = tmp_path / "export"
    with session_scope(factory) as s:
        entries = export_tables(s, out, DEFAULT_EXPORT_TABLES)
    assert set(entries) == {f"{t}.{ext}" for t in DEFAULT_EXPORT_TABLES for ext in ("csv", "jsonl")}
    for rel, entry in entries.items():
        assert isinstance(entry, FileEntry) and entry.path == rel
        data = (out / rel).read_bytes()
        assert b"\r" not in data
        assert not data.startswith(b"\xef\xbb\xbf")
        assert data == b"" or data.endswith(b"\n")

    invoices = _read_csv(out / "invoices.csv")
    assert list(invoices[0]) == [c.name for c in Base.metadata.tables["invoices"].columns]
    assert entries["invoices.csv"].rows == len(invoices)
    for row in invoices:
        for col in ("invoiced_amount", "expected_amount", "variance_amount"):
            assert MONEY_RE.match(row[col]), row[col]
        assert re.match(r"^-?\d+\.\d{6}$", row["variance_percentage"])
        assert TS_RE.match(row["recorded_at"])
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", row["invoice_date"])
    assert [r["invoice_id"] for r in invoices] == sorted(r["invoice_id"] for r in invoices)

    shipments = _read_csv(out / "shipments.csv")
    assert any(r["actual_delivery_timestamp"] == "" for r in shipments)  # NULL -> empty cell
    jsonl = (out / "shipments.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(jsonl[0])
    assert list(first) == [c.name for c in Base.metadata.tables["shipments"].columns]
    assert any(json.loads(line)["actual_delivery_timestamp"] is None for line in jsonl)
    assert all(MONEY_RE.match(json.loads(line)["shipped_quantity"]) for line in jsonl)

    reqs = _read_csv(out / "compliance_requirements.csv")
    assert {r["mandatory"] for r in reqs} == {"true", "false"}

    events = _read_csv(out / "delivery_events.csv")
    meta = [r["event_metadata"] for r in events if r["event_type"] == "CORRECTION"]
    assert meta
    for m in meta:
        assert m == json.dumps(json.loads(m), sort_keys=True, separators=(",", ":"))
    assert entries["supplier_scores.csv"].rows == 0


def test_export_requantises_sqlite_float_numeric(loaded, tmp_path):
    factory, _ = loaded
    with session_scope(factory) as s:
        first = s.scalar(sa.text("SELECT min(po_line_id) FROM po_lines"))
        s.execute(
            sa.text("UPDATE po_lines SET agreed_unit_price = 0.1 + 0.2 WHERE po_line_id = :i"),
            {"i": first},
        )
    with session_scope(factory) as s:
        export_tables(s, tmp_path, ["po_lines"])
    row = _read_csv(tmp_path / "po_lines.csv")[0]
    assert row["po_line_id"] == first
    assert row["agreed_unit_price"] == "0.30"


def test_export_is_deterministic(loaded, tmp_path):
    factory, _ = loaded
    with session_scope(factory) as s:
        a = export_tables(s, tmp_path / "a", ["suppliers", "risk_signals", "delivery_events"])
    with session_scope(factory) as s:
        b = export_tables(s, tmp_path / "b", ["suppliers", "risk_signals", "delivery_events"])
    assert a == b


# --- manifest -------------------------------------------------------------------------------


def _entries():
    return {
        "b.csv": FileEntry("b.csv", 2, "2" * 64),
        "a.csv": FileEntry("a.csv", 1, "1" * 64),
    }


def test_manifest_checksum_ignores_execution_metadata(starter):
    cfg, _ = starter
    counts = {"suppliers": 50}
    m1 = build_manifest(cfg, _entries(), counts, execution_metadata("2026-01-01T00:00:00Z", 1.5))
    meta2 = execution_metadata("2027-05-05T00:00:00Z", 99.0) | {"hostname": "elsewhere"}
    m2 = build_manifest(cfg, _entries(), counts, meta2)
    assert m1["execution_metadata"] != m2["execution_metadata"]
    assert m1["content_checksum"] == m2["content_checksum"]
    changed = _entries() | {"a.csv": FileEntry("a.csv", 1, "f" * 64)}
    m3 = build_manifest(cfg, changed, counts, meta2)
    assert m3["content_checksum"] != m1["content_checksum"]


def test_manifest_fields(starter):
    cfg, _ = starter
    m = build_manifest(cfg, _entries(), {"suppliers": 50}, execution_metadata("x", 0.0))
    assert m["dataset_id"] == cfg.dataset_id
    assert m["seed"] == cfg.seed and m["preset"] == "starter"
    assert m["generator_version"] == "1.0.0" and m["schema_version"] == "1"
    assert m["config"] == json.loads(cfg.canonical_json())
    assert m["provenance"] == {
        "synthetic": True,
        "license": "MIT (synthetic data generated by this repository)",
    }
    assert m["row_counts"] == {"suppliers": 50}
    assert [f["path"] for f in m["files"]] == ["a.csv", "b.csv"]
    pairs = json.dumps([["a.csv", "1" * 64], ["b.csv", "2" * 64]], separators=(",", ":"))
    assert m["content_checksum"] == hashlib.sha256(pairs.encode("utf-8")).hexdigest()
    meta = m["execution_metadata"]
    assert set(meta) == {
        "generated_at",
        "duration_seconds",
        "python_version",
        "dependency_versions",
        "hostname",
    }
    assert {"numpy", "pandas", "faker", "sqlalchemy"} <= set(meta["dependency_versions"])
    json.dumps(m)  # serialisable
