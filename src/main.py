from fastapi import FastAPI

from src.providers.hold import router as hold_router
from src.providers.subs import router as subs_router

app = FastAPI(title="LibertAI subscriptions")
app.include_router(hold_router)
app.include_router(subs_router)
