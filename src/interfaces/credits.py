import enum
from datetime import datetime
from enum import Enum
from typing import Literal, Annotated

from libertai_utils.chains.index import is_address_valid
from libertai_utils.interfaces.blockchain import LibertaiChain
from pydantic import BaseModel, Field, field_validator
from pydantic_core.core_schema import FieldValidationInfo

from src.config import config


class CreditTransactionProvider(str, Enum):
    ltai_base = "ltai_base"  # LTAI Base payments
    ltai_solana = "ltai_solana"  # LTAI Solana payments
    thirdweb = "thirdweb"
    voucher = "voucher"
    sol_solana = "sol_solana"  # SOL Solana payments


class CreditBalanceResponse(BaseModel):
    address: str
    balance: float


class CreditTransactionStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    error = "error"


class CreditTransactionResponse(BaseModel):
    id: str  # UUID as string
    transaction_hash: str | None
    amount: float
    amount_left: float
    provider: CreditTransactionProvider
    created_at: datetime
    expired_at: datetime | None
    is_active: bool
    status: CreditTransactionStatus


class CreditTransactionsResponse(BaseModel):
    address: str
    transactions: list[CreditTransactionResponse]


class ExpiredCreditTransaction(BaseModel):
    id: str  # UUID as string
    transaction_hash: str | None
    address: str
    expired_at: datetime | None


class ExpiredCreditTransactionsResponse(BaseModel):
    updated_count: int
    transactions: list[ExpiredCreditTransaction]


class ThirdwebTransactionDetails(BaseModel):
    transactionHash: str
    amountWei: str
    amount: str
    amountUSDCents: int
    completedAt: str


class ThirdwebPurchaseData(BaseModel):
    userAddress: str


class ThirdwebBuyWithCryptoWebhook(BaseModel):
    swapType: str
    source: ThirdwebTransactionDetails
    status: Literal["COMPLETED", "PENDING"]
    toAddress: str
    destination: ThirdwebTransactionDetails | None = None
    purchaseData: ThirdwebPurchaseData


class VoucherAddCreditsRequest(BaseModel):
    chain: LibertaiChain
    address: str
    amount: Annotated[float, Field(gt=0)]
    expired_at: datetime | None = None
    password: str

    @field_validator("address")
    def validate_address(cls, value, info: FieldValidationInfo):
        chain: LibertaiChain = info.data.get("chain")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value

    @field_validator("password")
    def valid_password(cls, password):
        if password not in config.VOUCHERS_PASSWORDS:
            raise ValueError("Given password isn't in the list of allowed passwords.")


class VoucherCreditsResponse(BaseModel):
    id: str  # UUID as string
    address: str
    amount: float
    amount_left: float
    expired_at: datetime | None
    created_at: datetime
    is_active: bool


class GetVouchersRequest(BaseModel):
    chain: LibertaiChain
    address: str
    password: str

    @field_validator("address")
    def validate_address(cls, value, info: FieldValidationInfo):
        chain: LibertaiChain = info.data.get("chain")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value

    @field_validator("password")
    def valid_password(cls, password):
        if password not in config.VOUCHERS_PASSWORDS:
            raise ValueError("Given password isn't in the list of allowed passwords.")


class VoucherChangeExpireRequest(BaseModel):
    voucher_id: str
    expired_at: datetime | None
    password: str

    @field_validator("password")
    def valid_password(cls, password):
        if password not in config.VOUCHERS_PASSWORDS:
            raise ValueError("Given password isn't in the list of allowed passwords.")
