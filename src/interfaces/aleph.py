from pydantic import BaseModel


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
    hf_id: str
    capabilities: dict[str, TextCapability | bool]  # bool for image capability
    pricing: dict[str, TextPricing | float]  # float for image pricing


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class AlephAPIResponse(BaseModel):
    data: dict[str, ModelsResponse]
