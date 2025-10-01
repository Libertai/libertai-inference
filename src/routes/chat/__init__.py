from fastapi import APIRouter

router = APIRouter(prefix="/chat", tags=["Chat"])

# Import routes
from src.routes.chat.proxy import proxy_chat_request  # noqa
