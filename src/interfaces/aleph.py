from enum import StrEnum

from pydantic import BaseModel, model_validator


class TextCapability(BaseModel):
    context_window: int
    function_calling: bool
    reasoning: bool


class TextPricing(BaseModel):
    price_per_million_input_tokens: float
    price_per_million_output_tokens: float


class ModelInfo(BaseModel):
    id: str
    name: str
    hf_id: str | None = None
    capabilities: dict[str, TextCapability | bool]  # bool for image/search capability
    pricing: dict[str, TextPricing | float]  # float for image/search pricing


class RedirectionType(StrEnum):
    DEPRECATED = "DEPRECATED"
    INTERNAL = "INTERNAL"


class ModelRedirection(BaseModel):
    from_id: str
    to: str
    type: RedirectionType
    description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def rename_from(cls, data):
        if isinstance(data, dict) and "from" in data:
            data["from_id"] = data.pop("from")
        return data


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    redirections: list[ModelRedirection] = []


class AlephAPIResponse(BaseModel):
    data: dict[str, ModelsResponse]
