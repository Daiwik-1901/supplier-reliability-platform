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
