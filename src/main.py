from fastapi import FastAPI
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.subscription import GetUserSubscriptionsResponse, BaseSubscription, SubscriptionChain
from starlette.middleware.cors import CORSMiddleware

from src.providers.hold import router as hold_router
from src.providers.subs import router as subs_router
from src.providers.vouchers import router as vouchers_router
from src.utils.subscription import fetch_subscriptions

app = FastAPI(title="LibertAI subscriptions")

origins = [
    "https://chat.libertai.io",
    "http://localhost:9000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/subscriptions", tags=["General"])
async def get_user_subscriptions(address: str, chain: SubscriptionChain) -> GetUserSubscriptionsResponse:
    formatted_address = format_address(address, chain)
    subscriptions = await fetch_subscriptions([formatted_address])

    return GetUserSubscriptionsResponse(subscriptions=[BaseSubscription(**sub.dict()) for sub in subscriptions])


app.include_router(hold_router)
app.include_router(subs_router)
app.include_router(vouchers_router)
