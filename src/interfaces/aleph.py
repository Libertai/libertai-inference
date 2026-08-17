from pydantic import BaseModel


class TextCapability(BaseModel):
    context_window: int
    function_calling: bool
    reasoning: bool


class TextPricing(BaseModel):
    price_per_million_input_tokens: float
    price_per_million_output_tokens: float
    # Dedicated rate for input tokens served from the prefix cache. When unset, cached
    # input tokens fall back to the full input rate (no discount).
    price_per_million_cached_input_tokens: float | None = None


class EmbeddingCapability(BaseModel):
    context_window: int
    dimensions: int


class EmbeddingPricing(BaseModel):
    price_per_million_input_tokens: float


class AudioCapability(BaseModel):
    languages: list[str]
    voices: list[str]


class AudioPricing(BaseModel):
    price_per_million_input_characters: float


class ModelInfo(BaseModel):
    id: str
    name: str
    hf_id: str | None = None
    # bool for image/search capability
    capabilities: dict[str, TextCapability | EmbeddingCapability | AudioCapability | bool]
    # float for image/search pricing
    pricing: dict[str, TextPricing | EmbeddingPricing | AudioPricing | float]


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class AlephAPIResponse(BaseModel):
    data: dict[str, ModelsResponse]
