from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.routes.api_keys import router as api_keys_router
from src.routes.auth import router as auth_router
from src.routes.credits import router as credits_router
from src.utils.cron import lifespan

app = FastAPI(title="LibertAI inference", lifespan=lifespan)

# Add security scheme to OpenAPI documentation
app.openapi_components = {  # type: ignore
    "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}
}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth_router)
app.include_router(credits_router)
app.include_router(api_keys_router)
