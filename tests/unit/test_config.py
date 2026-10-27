import copy
import json
import os

import pytest
from pydantic import ValidationError

from supplier_reliability import SCORING_VERSION
from supplier_reliability.config import AppConfig, Settings, load_app_config


def _data(app_config: AppConfig) -> dict:
    return copy.deepcopy(app_config.model_dump(mode="json"))


def test_default_weights_sum_to_one(app_config):
    assert abs(sum(app_config.scoring.weights.values()) - 1.0) < 1e-9
    assert app_config.scoring.weights["delivery"] == 0.30


def test_negative_weight_rejected(app_config):
    data = _data(app_config)
    data["scoring"]["weights"]["cost"] = -0.15
    data["scoring"]["weights"]["delivery"] = 0.60  # still sums to 1
    with pytest.raises(ValidationError, match="nonnegative"):
        AppConfig.model_validate(data)


def test_weights_not_summing_to_one_rejected(app_config):
    data = _data(app_config)
    data["scoring"]["weights"]["cost"] = 0.20
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_threshold_gap_rejected(app_config):
    data = _data(app_config)
    data["scoring"]["category_thresholds"]["ACCEPTABLE"]["lower"] = 71
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_threshold_not_covering_zero_to_hundred_rejected(app_config):
    data = _data(app_config)
    data["scoring"]["category_thresholds"]["CRITICAL"]["lower"] = 5
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_profile_shares_must_sum_to_one(app_config):
    assert abs(sum(p.share for p in app_config.generator.profiles.values()) - 1.0) < 1e-9
    data = _data(app_config)
    data["generator"]["profiles"]["average"]["share"] = 0.30
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_hash_stable(app_config, config_path):
    assert app_config.config_hash() == load_app_config(config_path).config_hash()


def test_config_hash_changes_with_parameters(app_config):
    data = _data(app_config)
    data["scoring"]["grace_hours"] = 48
    assert AppConfig.model_validate(data).config_hash() != app_config.config_hash()


def test_default_yaml_carries_required_parameters(app_config):
    s = app_config.scoring
    assert s.scoring_version == "1.0.0"
    assert s.min_observations == {"delivery": 5, "quality": 3, "cost": 3}
    assert s.delivery.delay_penalty_per_day == 2 and s.delivery.delay_penalty_cap == 20
    assert s.severity_weights["CRITICAL"] == 50
    assert app_config.generator.seed == 20261022
    assert app_config.generator.presets["full"]["inspections"] == 20000
    assert "recovering" in app_config.generator.profiles
    assert app_config.anomalies.full.negatives["scores"] == 300


def test_settings_defaults_and_env_prefix(monkeypatch):
    for key in [k for k in os.environ if k.startswith("SRP_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SRP_MAX_PAGE_SIZE", "42")
    monkeypatch.setenv("SRP_CORS_ORIGINS", '["http://a.example"]')
    settings = Settings(_env_file=None)
    assert settings.max_page_size == 42
    assert settings.cors_origins == ["http://a.example"]
    assert settings.app_env == "local"


def test_canonical_json_has_no_whitespace(app_config):
    assert " " not in app_config.canonical_json()
    assert json.loads(app_config.canonical_json())["scoring"]["grace_hours"] == 24


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("category_thresholds", "AT_RISK"),
        ("min_observations", "cost"),
        ("compliance_points", "EXPIRED"),
        ("severity_weights", "CRITICAL"),
    ],
)
def test_missing_map_key_rejected(app_config, field, key):
    data = _data(app_config)
    del data["scoring"][field][key]
    with pytest.raises(ValidationError, match="exactly"):
        AppConfig.model_validate(data)


def test_profile_without_share_is_validation_error(app_config):
    data = _data(app_config)
    del data["generator"]["profiles"]["average"]["share"]
    with pytest.raises(ValidationError, match="share"):
        AppConfig.model_validate(data)


def test_scoring_version_matches_package_constant(app_config):
    assert app_config.scoring.scoring_version == SCORING_VERSION
