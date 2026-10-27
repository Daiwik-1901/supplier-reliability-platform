"""Alembic migration tests: the migrated schema must be exactly what the models define."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from supplier_reliability.database import alembic_config, make_engine, run_migrations
from supplier_reliability.models import Base

EXPECTED_TABLES = {
    "suppliers",
    "sites",
    "contracts",
    "contract_prices",
    "compliance_requirements",
    "supplier_certifications",
    "capacity_declarations",
    "purchase_orders",
    "po_lines",
    "shipments",
    "shipment_lines",
    "delivery_events",
    "inspections",
    "invoices",
    "risk_signals",
    "supplier_scores",
    "score_evidence",
    "scoring_config_snapshots",
    "validation_runs",
    "validation_findings",
    "data_exceptions",
    "raw_ingestion_records",
    "dataset_manifests",
    "dataset_jobs",
}


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


@pytest.fixture
def created_engine(tmp_path: Path):
    """Schema built by Base.metadata.create_all (reference for structural comparison)."""
    engine = make_engine(_sqlite_url(tmp_path / "created.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_upgrade_creates_all_tables(migrated_engine):
    names = set(sa.inspect(migrated_engine).get_table_names())
    assert names >= EXPECTED_TABLES
    assert names - {"alembic_version"} == set(Base.metadata.tables)


def test_migration_matches_models(migrated_engine):
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(ctx, Base.metadata) == []


def _structure(engine) -> dict:
    insp = sa.inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]), str(c["default"]))
                for c in insp.get_columns(table)
            },
            "pk": insp.get_pk_constraint(table)["constrained_columns"],
            "unique": sorted(
                (u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(table)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(table)
            ),
            "fks": sorted(
                (
                    f["name"],
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                )
                for f in insp.get_foreign_keys(table)
            ),
            "checks": sorted(c["name"] for c in insp.get_check_constraints(table)),
        }
    return out


def test_migrated_structure_equals_create_all(migrated_engine, created_engine):
    migrated, created = _structure(migrated_engine), _structure(created_engine)
    assert migrated.keys() == created.keys()
    for table in created:
        assert migrated[table] == created[table], table
    assert any(t["checks"] for t in created.values())  # guard: CHECK names are compared


def _index_and_table_sql(engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('index','table') AND sql IS NOT NULL"
            )
        ).all()
    return {name: sql for name, sql in rows}


def _normalise(sql: str) -> str:
    return " ".join(sql.replace('"', "").replace("`", "").split())


def test_partial_index_where_clauses_match(migrated_engine, created_engine):
    migrated = _index_and_table_sql(migrated_engine)
    created = _index_and_table_sql(created_engine)
    partial = {n: s for n, s in created.items() if "CREATE UNIQUE INDEX" in s and "WHERE" in s}
    assert partial  # guard: partial unique indexes exist
    for name, sql in partial.items():
        assert _normalise(migrated[name]) == _normalise(sql), name


def test_postgres_ddl_compiles():
    dialect = postgresql.dialect()
    names: list[str] = []
    partial = 0
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        names.append(table.name)
        names.extend(c.name for c in table.columns)
        names.extend(str(c.name) for c in table.constraints if c.name)
        for index in table.indexes:
            ddl = str(CreateIndex(index).compile(dialect=dialect))
            names.append(index.name)
            if index.dialect_options["postgresql"]["where"] is not None:
                assert "WHERE" in ddl
                partial += 1
    assert partial > 0
    too_long = [n for n in names if len(n) > 63]
    assert not too_long, too_long


def test_downgrade_to_base_and_upgrade_again(tmp_path: Path):
    url = _sqlite_url(tmp_path / "roundtrip.db")
    run_migrations(url)
    command.downgrade(alembic_config(url), "base")
    engine = make_engine(url)
    try:
        assert set(sa.inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
    run_migrations(url)
    engine = make_engine(url)
    try:
        assert set(sa.inspect(engine).get_table_names()) - {"alembic_version"} == set(
            Base.metadata.tables
        )
    finally:
        engine.dispose()
