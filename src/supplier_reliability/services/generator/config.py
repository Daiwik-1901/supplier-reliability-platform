"""Typed generator configuration, built from AppConfig (single source of truth: YAML)."""

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from supplier_reliability.config import AnomalyConfig, AnomalyPreset, AppConfig, ProfileParams

__all__ = [
    "AnomalyConfig",
    "AnomalyPreset",
    "GeneratorCapacityError",
    "GeneratorConfig",
    "ProfileParams",
]


class GeneratorCapacityError(ValueError):
    """Requested counts cannot be satisfied (e.g. minimums exceed totals)."""


class GeneratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    preset: str
    n_suppliers: int
    n_purchase_orders: int
    n_shipments: int
    n_inspections: int
    reference_start: datetime
    reference_end: datetime
    profiles: dict[str, ProfileParams]
    anomaly_preset: AnomalyPreset
    e_cross_supplier_fraction: float

    @classmethod
    def from_app_config(
        cls, app_config: AppConfig, preset: str, seed: int | None = None
    ) -> "GeneratorConfig":
        gen = app_config.generator
        if preset not in gen.presets:
            raise ValueError(f"unknown preset {preset!r}; known: {sorted(gen.presets)}")
        counts = gen.presets[preset]
        anomaly_preset = getattr(app_config.anomalies, preset, None)
        if not isinstance(anomaly_preset, AnomalyPreset):
            raise ValueError(f"no anomaly configuration for preset {preset!r}")
        return cls(
            seed=gen.seed if seed is None else seed,
            preset=preset,
            n_suppliers=counts["suppliers"],
            n_purchase_orders=counts["purchase_orders"],
            n_shipments=counts["shipments"],
            n_inspections=counts["inspections"],
            reference_start=datetime.fromisoformat(gen.reference_period["start"]),
            reference_end=datetime.fromisoformat(gen.reference_period["end"]),
            profiles=gen.profiles,
            anomaly_preset=anomaly_preset,
            e_cross_supplier_fraction=app_config.anomalies.e_cross_supplier_fraction,
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def dataset_id(self) -> str:
        return f"DS-{self.preset}-{self.seed}-{self.config_hash()[:8]}"
