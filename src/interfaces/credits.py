import enum
from datetime import datetime
from enum import Enum
from typing import Annotated

from libertai_utils.chains.index import is_address_valid
from libertai_utils.interfaces.blockchain import LibertaiChain
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


class CreditTransactionProvider(str, Enum):
    ltai_base = "ltai_base"  # LTAI Base payments
    ltai_solana = "ltai_solana"  # LTAI Solana payments
    thirdweb = "thirdweb"
    voucher = "voucher"
    sol_solana = "sol_solana"  # SOL Solana payments
    revolut = "revolut"  # Fiat (card/bank) top-ups via Revolut


class CreditBalanceResponse(BaseModel):
    address: str | None
    balance: float


class CreditTransactionStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    error = "error"


class CreditTransactionResponse(BaseModel):
    id: str  # UUID as string
    external_reference: str | None
    amount: float
    amount_left: float
    provider: CreditTransactionProvider
    created_at: datetime
    expired_at: datetime | None
    is_active: bool
    status: CreditTransactionStatus


class CreditTransactionsResponse(BaseModel):
    address: str | None
    transactions: list[CreditTransactionResponse]


class ExpiredCreditTransaction(BaseModel):
    id: str  # UUID as string
    external_reference: str | None
    address: str | None
    expired_at: datetime | None


class ExpiredCreditTransactionsResponse(BaseModel):
    updated_count: int
    transactions: list[ExpiredCreditTransaction]


class ThirdwebPurchaseData(BaseModel):
    # The session user id — credits land on the signed-in account regardless of which wallet paid
    # (email/OAuth users pay via a just-connected wallet that isn't their own account).
    userId: str


# Only the fields the handlers act on: Thirdweb sends a superset that varies per token and
# chain, and a field we never read must not fail validation.
class ThirdwebToken(BaseModel):
    chainId: int
    symbol: str
    decimals: int


class ThirdwebTransactionReference(BaseModel):
    chainId: int
    transactionHash: str


class ThirdwebOnchainTransactionData(BaseModel):
    status: str
    destinationToken: ThirdwebToken
    destinationAmount: str
    receiver: str
    transactions: list[ThirdwebTransactionReference]
    purchaseData: ThirdwebPurchaseData


class ThirdwebOnrampTransactionData(BaseModel):
    id: str
    token: ThirdwebToken
    amount: str
    receiver: str
    status: str
    purchaseData: ThirdwebPurchaseData


class VoucherAddCreditsRequest(BaseModel):
    amount: Annotated[float, Field(gt=0)]
    expired_at: datetime | None = None
    # Exactly one recipient: a wallet (chain + address) or an email account.
    chain: LibertaiChain | None = None
    address: str | None = None
    email: str | None = None

    @field_validator("address")
    def validate_address(cls, value, info: ValidationInfo):
        if value is None:
            return value
        chain: LibertaiChain | None = info.data.get("chain")
        if chain is None:
            raise ValueError("chain is required when address is provided")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value

    @model_validator(mode="after")
    def exactly_one_recipient(self):
        if (self.address is not None) == (self.email is not None):
            raise ValueError("Provide exactly one recipient: a wallet (chain + address) or an email.")
        return self


class VoucherCreditsResponse(BaseModel):
    id: str  # UUID as string
    address: str | None
    amount: float
    amount_left: float
    expired_at: datetime | None
    created_at: datetime
    is_active: bool


class GetVouchersRequest(BaseModel):
    chain: LibertaiChain
    address: str

    @field_validator("address")
    def validate_address(cls, value, info: ValidationInfo):
        chain: LibertaiChain = info.data.get("chain")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value


class VoucherChangeExpireRequest(BaseModel):
    voucher_id: str
    expired_at: datetime | None
