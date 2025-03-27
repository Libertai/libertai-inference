from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.routes.api_keys import router as api_keys_router
from src.routes.credits import router as credits_router
from src.utils.cron import lifespan

app = FastAPI(title="LibertAI inference", lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(credits_router)
app.include_router(api_keys_router)
