from fastapi import APIRouter

router = APIRouter(prefix="/api-keys", tags=["API Keys"])

from src.routes.api_keys.api_keys import (  # noqa
    create_api_key,
    get_api_keys,
    get_api_key,
    update_api_key,
    delete_api_key,
    log_api_key_usage,
    get_api_key_usage_stats,
)
