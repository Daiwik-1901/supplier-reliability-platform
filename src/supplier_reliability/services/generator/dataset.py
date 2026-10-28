"""In-memory generated dataset: one list of row dicts per table (keys = model column names)."""

from dataclasses import dataclass, field
from typing import Any

from supplier_reliability.services.generator.config import GeneratorConfig

Rows = list[dict[str, Any]]


@dataclass
class GeneratedDataset:
    config: GeneratorConfig
    suppliers: Rows = field(default_factory=list)
    sites: Rows = field(default_factory=list)
    contracts: Rows = field(default_factory=list)
    contract_prices: Rows = field(default_factory=list)
    compliance_requirements: Rows = field(default_factory=list)
    supplier_certifications: Rows = field(default_factory=list)
    capacity_declarations: Rows = field(default_factory=list)
    purchase_orders: Rows = field(default_factory=list)
    po_lines: Rows = field(default_factory=list)
    shipments: Rows = field(default_factory=list)
    shipment_lines: Rows = field(default_factory=list)
    delivery_events: Rows = field(default_factory=list)
    inspections: Rows = field(default_factory=list)
    invoices: Rows = field(default_factory=list)
    risk_signals: Rows = field(default_factory=list)
    # Ground truth only: never copied into supplier rows.
    supplier_profiles: dict[str, str] = field(default_factory=dict)


def generate_dataset(cfg: GeneratorConfig) -> GeneratedDataset:
    """Generate every table, parents first (spec section 7.4): master data -> orders, shipments
    and events -> inspections and invoices -> risk signals. Deterministic in ``cfg``."""
    # Local imports: every stage module imports GeneratedDataset from this module.
    from supplier_reliability.services.generator.ids import IdRegistry
    from supplier_reliability.services.generator.master_data import generate_master_data
    from supplier_reliability.services.generator.quality_finance import (
        generate_inspections_and_invoices,
    )
    from supplier_reliability.services.generator.risk import generate_risk_signals
    from supplier_reliability.services.generator.rng import RandomSource
    from supplier_reliability.services.generator.transactions import (
        generate_orders_and_shipments,
    )

    rs, ids, ds = RandomSource(cfg.seed), IdRegistry(), GeneratedDataset(config=cfg)
    generate_master_data(cfg, rs, ids, ds)
    generate_orders_and_shipments(cfg, rs, ids, ds)
    generate_inspections_and_invoices(cfg, rs, ids, ds)
    generate_risk_signals(cfg, rs, ids, ds)
    return ds
