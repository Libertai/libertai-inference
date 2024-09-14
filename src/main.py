from fastapi import FastAPI

from src.providers.hold import router as hold_router

app = FastAPI(title="LibertAI subscriptions")
app.include_router(hold_router)
