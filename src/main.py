from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import config
from src.routes.agents import router as agents_router
from src.routes.api_keys import router as api_keys_router
from src.routes.auth import router as auth_router
from src.routes.chat import router as chat_router
from src.routes.credits import router as credits_router
from src.routes.stats import router as stats_router
from src.routes.subscriptions import router as subscriptions_router
from src.utils.cron import lifespan

app = FastAPI(title="LibertAI inference", lifespan=lifespan)

# Add security scheme to OpenAPI documentation
app.openapi_components = {  # type: ignore
    "securitySchemes": {"CookieAuth": {"type": "apiKey", "in": "cookie", "name": "libertai_auth"}}
}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://console.libertai.io", "https://analytics.libertai.io"]
    + (["http://localhost:5173", "http://localhost:3000"] if config.IS_DEVELOPMENT else []),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,  # Required for cookies to be sent with requests
)


app.include_router(auth_router)
app.include_router(credits_router)
app.include_router(api_keys_router)
app.include_router(subscriptions_router)
app.include_router(agents_router)
app.include_router(stats_router)
app.include_router(chat_router)
