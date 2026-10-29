"""Schemas for the evaluation run. The report endpoints return the Task 17 report payloads
as-is (JSON objects), so they have no model here."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ConfusionOut(BaseModel):
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None


class AnomalyEvaluationOut(BaseModel):
    unit: str
    per_class: dict[str, ConfusionOut]
    micro: ConfusionOut
    negative_population: dict[str, int]


class ReconciliationOut(BaseModel):
    tolerance: str
    checked: int
    matched: int
    mismatches_count: int
    mismatches: list[dict[str, Any]]  # at most the first 10
    missing_persisted: int


class EvaluationRunOut(BaseModel):
    dataset_id: str
    anomaly_evaluation: AnomalyEvaluationOut
    reconciliation: ReconciliationOut

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "dataset_id": "DS-starter-20261022-1a2b3c4d",
                    "anomaly_evaluation": {
                        "unit": "raw record per issue class",
                        "per_class": {
                            "DELIVERY_BEFORE_DISPATCH": {
                                "tp": 5,
                                "fp": 0,
                                "fn": 0,
                                "tn": 40,
                                "precision": 1.0,
                                "recall": 1.0,
                                "f1": 1.0,
                            }
                        },
                        "micro": {
                            "tp": 5,
                            "fp": 0,
                            "fn": 0,
                            "tn": 40,
                            "precision": 1.0,
                            "recall": 1.0,
                            "f1": 1.0,
                        },
                        "negative_population": {"DELIVERY_BEFORE_DISPATCH": 40},
                    },
                    "reconciliation": {
                        "tolerance": "0",
                        "checked": 60,
                        "matched": 60,
                        "mismatches_count": 0,
                        "mismatches": [],
                        "missing_persisted": 0,
                    },
                }
            ]
        },
    )
