import httpx
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from src.config import config
from src.interfaces.chat import AnonUsageResponse
from src.models.base import AsyncSessionLocal
from src.routes.chat import router
from src.services import anon_rate_limit
from src.services.geo import client_ip
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

timeout = httpx.Timeout(timeout=600.0)  # 10 minutes
client = httpx.AsyncClient(timeout=timeout)


def _anon_usage_response(state: anon_rate_limit.AnonUsageState) -> AnonUsageResponse:
    return AnonUsageResponse(
        used=state.used,
        limit=state.limit,
        allowed=state.allowed,
        resets_at=state.resets_at.isoformat() if state.resets_at else None,
    )


class ChatRequest(BaseModel):
    model: str

    model_config = ConfigDict(extra="allow")  # allow extra fields


async def shutdown_event():
    await client.aclose()


@router.post("/completions")  # type: ignore
async def proxy_chat_request(
    request: Request,
    chat_request_data: ChatRequest,
):
    """
    Proxy requests to LibertAI chat completions API.

    Always replaces the Authorization header with LIBERTAI_CHAT_DEFAULT_API_KEY from environment.
    Forwards all request parameters, query params, and body to api.libertai.io/v1/chat/completions.

    Handles both streaming and non-streaming responses.
    """
    logger.debug(f"Received chat request for model {chat_request_data.model}")

    # Only anonymous (logged-out) traffic hits this proxy — authenticated users go straight to the
    # gateway with their per-user key. Rate-limit anonymous messages per IP to nudge sign-in.
    ip = client_ip(request)
    if ip:
        async with AsyncSessionLocal() as db:
            state = await anon_rate_limit.consume(db, ip)
        if not state.allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "anon_limit", **_anon_usage_response(state).model_dump()},
            )

    # Get the original request body & headers
    headers = dict(request.headers)
    body = await request.body()

    api_key = config.LIBERTAI_CHAT_API_KEY
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LIBERTAI_CHAT_API_KEY not configured",
        )

    # Replace authorization header with our API key
    headers["authorization"] = f"Bearer {api_key}"

    # Clean up headers that shouldn't be forwarded
    headers.pop("host", None)
    headers.pop("content-length", None)  # Let httpx calculate this

    # Forward the request to LibertAI API
    try:
        req = client.build_request(
            "POST",
            f"{config.LIBERTAI_CHAT_API_BASE_URL}/v1/chat/completions",
            content=body,
            headers=headers,
            params=request.query_params,
        )
        response = await client.send(req, stream=True)

        # Check for error responses
        if response.status_code >= 400:
            error_content = await response.aread()
            await response.aclose()
            logger.error(f"LibertAI API error: {response.status_code} - {error_content.decode()}")
            return Response(
                content=error_content,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.headers.get("Content-Type", "application/json"),
            )

        # Check if it's a streaming response
        is_streaming_response = response.headers.get("content-type", "") == "text/event-stream"

        if is_streaming_response:

            async def generate_chunks():
                try:
                    async for chunk in response.aiter_bytes():
                        yield chunk
                finally:
                    await response.aclose()

            return StreamingResponse(
                content=generate_chunks(),
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.headers.get("Content-Type", "text/event-stream"),
            )
        else:
            response_bytes = await response.aread()
            await response.aclose()

            return Response(
                content=response_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.headers.get("Content-Type", "application/json"),
            )

    except httpx.HTTPError as e:
        logger.error(f"Error forwarding request to LibertAI API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error forwarding request to LibertAI API: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Unexpected error in chat proxy: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}",
        )


@router.get("/anon-usage")  # type: ignore
async def get_anon_usage(request: Request) -> AnonUsageResponse:
    """Anonymous per-IP free-message usage, so the logged-out chat UI can show remaining messages,
    a near-limit warning, and the sign-in wall before the next message is rejected."""
    ip = client_ip(request)
    if not ip:
        return AnonUsageResponse(used=0, limit=anon_rate_limit.ANON_MESSAGE_LIMIT, allowed=True, resets_at=None)
    async with AsyncSessionLocal() as db:
        state = await anon_rate_limit.get_state(db, ip)
    return _anon_usage_response(state)
