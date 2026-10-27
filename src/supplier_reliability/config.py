"""Environment settings (SRP_*) and the YAML application config."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ComponentName = Literal["delivery", "quality", "cost", "capacity", "compliance", "disruption"]
COMPONENT_NAMES: tuple[str, ...] = (
    "delivery",
    "quality",
    "cost",
    "capacity",
    "compliance",
    "disruption",
)
TOLERANCE = 1e-9


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SRP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    app_env: str = "local"
    log_level: str = "INFO"
    log_json: bool = False
    config_path: Path = Path("config/default.yaml")
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    max_page_size: int = 500
    cors_origins: list[str] = []


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryThreshold(_Strict):
    lower: float
    upper: float


class DeliveryConfig(_Strict):
    delay_penalty_per_day: float = 2
    delay_penalty_cap: float = 20


class CapacityBreakpoint(_Strict):
    utilization: float
    score: float


class ScoringConfig(_Strict):
    scoring_version: str
    weights: dict[ComponentName, float]
    category_thresholds: dict[str, CategoryThreshold]
    grace_hours: int = 24
    min_observations: dict[str, int]
    coverage_threshold: float = 0.70
    delivery: DeliveryConfig
    defect_rate_zero_score: float = 0.10
    variance_zero_score: float = 0.10
    underbilling_weight: float = 0.5
    capacity_breakpoints: list[CapacityBreakpoint]
    compliance_points: dict[str, float]
    severity_weights: dict[str, float]
    recency_days: int = 90
    lookback_days: int = 180
    resolved_multiplier: float = 0.5
    inactive_site_multiplier: float = 0.5
    dedup_window_days: int = 7
    deteriorating_threshold: float = 10

    @model_validator(mode="after")
    def _check_weights(self) -> "ScoringConfig":
        if set(self.weights) != set(COMPONENT_NAMES):
            raise ValueError(f"weights must define exactly {list(COMPONENT_NAMES)}")
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("weights must be nonnegative")
        if abs(sum(self.weights.values()) - 1.0) > TOLERANCE:
            raise ValueError("weights must sum to 1")
        return self

    @model_validator(mode="after")
    def _check_thresholds(self) -> "ScoringConfig":
        bands = sorted(self.category_thresholds.values(), key=lambda b: b.lower)
        if not bands or bands[0].lower != 0 or bands[-1].upper != 100:
            raise ValueError("category thresholds must cover [0, 100]")
        for band in bands:
            if band.upper <= band.lower:
                raise ValueError("category threshold upper must exceed lower")
        for prev, nxt in zip(bands, bands[1:], strict=False):
            if prev.upper != nxt.lower:
                raise ValueError("category thresholds must be gap-free and non-overlapping")
        return self


class GeneratorSettingsBlock(_Strict):
    """Generator parameters. Loosely typed here; Task 5 defines the typed GeneratorConfig."""

    seed: int
    reference_period: dict[str, str]
    presets: dict[str, dict[str, int]]
    profiles: dict[str, dict[str, Any]]

    @model_validator(mode="after")
    def _check_profile_shares(self) -> "GeneratorSettingsBlock":
        total = sum(p["share"] for p in self.profiles.values())
        if abs(total - 1.0) > TOLERANCE:
            raise ValueError(f"profile shares must sum to 1, got {total}")
        return self


class AnomalyPreset(_Strict):
    positives_per_class: int
    negatives: dict[str, int]


class AnomalyConfig(_Strict):
    e_cross_supplier_fraction: float
    full: AnomalyPreset
    starter: AnomalyPreset


class AppConfig(_Strict):
    scoring: ScoringConfig
    generator: GeneratorSettingsBlock
    anomalies: AnomalyConfig

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_app_config(path: Path) -> AppConfig:
    with Path(path).open(encoding="utf-8") as fh:
        return AppConfig.model_validate(yaml.safe_load(fh))


@lru_cache
def get_settings() -> Settings:
    return Settings()
