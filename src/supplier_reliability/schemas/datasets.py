"""Schemas for dataset generation jobs."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from supplier_reliability.domain.enums import JobStatus
from supplier_reliability.schemas.common import OrmModel, UtcDatetime

PresetStr = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]


class GenerateRequest(BaseModel):
    preset: PresetStr = Field(
        description='Generator preset configured in the YAML config, "starter" or "full".'
    )
    seed: int | None = Field(
        default=None, ge=0, le=2**63 - 1, description="Random seed; defaults to the configured one."
    )
    replace_existing: bool = Field(
        default=False,
        description="Replace data already in the database (job history is kept).",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"preset": "starter"},
                {"preset": "full", "seed": 20261022, "replace_existing": True},
            ]
        },
    )


class JobAcceptedOut(BaseModel):
    job_id: str
    status: JobStatus

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"job_id": "JOB-000001", "status": "QUEUED"}]}
    )


class JobOut(OrmModel):
    job_id: str
    status: JobStatus
    preset: str
    seed: int
    requested_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    error_message: str | None = Field(
        description="Sanitised failure description (class names only); null unless FAILED."
    )
    dataset_id: str | None
    current_stage: str | None = Field(
        default=None,
        description="Last pipeline stage reported by a job of this server process "
        "(best effort; absent after a restart).",
    )

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "job_id": "JOB-000001",
                    "status": "SUCCEEDED",
                    "preset": "starter",
                    "seed": 20261022,
                    "requested_at": "2026-10-28T09:00:00Z",
                    "started_at": "2026-10-28T09:00:00Z",
                    "finished_at": "2026-10-28T09:00:14Z",
                    "error_message": None,
                    "dataset_id": "DS-starter-20261022-1a2b3c4d",
                    "current_stage": "manifest",
                }
            ]
        },
    )
