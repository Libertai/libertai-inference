from enum import Enum
from typing import Literal

from pydantic import BaseModel


class CreditTransactionProvider(str, Enum):
    libertai = "libertai"
    thirdweb = "thirdweb"


class ThirdwebTransactionDetails(BaseModel):
    transactionHash: str
    amountWei: str
    amount: str
    amountUSDCents: int
    completedAt: str


class ThirdwebBuyWithCryptoWebhook(BaseModel):
    swapType: str
    source: ThirdwebTransactionDetails
    status: Literal["COMPLETED", "PENDING"]
    fromAddress: str
    toAddress: str
    destination: ThirdwebTransactionDetails | None = None
