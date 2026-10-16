"""Public request/result contracts only; no backend implementation."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WODScalingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    original_wod: str = Field(min_length=1, max_length=5_000)
    available_equipment: list[str] = Field(max_length=50)
    target_time_domain_mins: int | None = Field(default=15, ge=1, le=240)
    athlete_notes: str | None = Field(default=None, max_length=1_000)

    @field_validator("original_wod")
    @classmethod
    def reject_blank_wod(cls, value: str) -> str:
        if not value:
            raise ValueError("original_wod must not be blank")
        return value

    @field_validator("available_equipment")
    @classmethod
    def normalize_equipment(cls, equipment: list[str]) -> list[str]:
        normalized = [item.strip() for item in equipment]
        if any(not item for item in normalized):
            raise ValueError("available_equipment items must not be blank")
        return normalized


class WODScalingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scaled_wod: str = Field(min_length=1)
    movement_substitutions: list[str]
    target_stimulus: str = Field(min_length=1)
    execution_time_ms: float = Field(ge=0, allow_inf_nan=False)
