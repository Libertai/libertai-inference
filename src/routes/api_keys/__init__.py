from fastapi import APIRouter

router = APIRouter(prefix="/api-keys", tags=["API Keys"])

from src.routes.api_keys.api_keys import (  # noqa
    create_api_key,
    get_api_keys,
    update_api_key,
    delete_api_key,
    register_inference_call,
)
