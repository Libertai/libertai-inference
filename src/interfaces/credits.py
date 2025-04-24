from datetime import datetime
from enum import Enum
from typing import Literal, Annotated

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from src.config import config


class CreditTransactionProvider(str, Enum):
    libertai = "libertai"
    thirdweb = "thirdweb"
    voucher = "voucher"


class CreditBalanceResponse(BaseModel):
    address: str
    balance: float


class ExpiredCreditTransaction(BaseModel):
    id: str  # UUID as string
    transaction_hash: str | None
    address: str
    expired_at: datetime | None


class ExpiredCreditTransactionsResponse(BaseModel):
    updated_count: int
    transactions: list[ExpiredCreditTransaction]


class ThirdwebToken(BaseModel):
    chainId: int
    address: str
    symbol: str
    name: str
    decimals: int
    priceUsd: float
    iconUri: str


class ThirdwebTransaction(BaseModel):
    transactionHash: str
    chainId: int


class ThirdwebPurchaseData(BaseModel):
    userAddress: str


class ThirdwebBuyWithCryptoWebhook(BaseModel):
    transactionId: str
    paymentId: str
    clientId: str
    action: Literal["BUY"]
    status: Literal["COMPLETED", "PENDING"]
    originToken: ThirdwebToken
    originAmount: str
    destinationToken: ThirdwebToken
    destinationAmount: str
    sender: str
    receiver: str
    type: str
    transactions: list[ThirdwebTransaction]
    developerFeeBps: int
    developerFeeRecipient: str
    purchaseData: ThirdwebPurchaseData


class VoucherAddCreditsRequest(BaseModel):
    address: str
    amount: Annotated[float, Field(gt=0)]
    expired_at: datetime | None = None
    password: str

    @field_validator("address")
    def validate_eth_address(cls, value):
        return Web3.to_checksum_address(value)

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
    address: str
    password: str

    @field_validator("address")
    def validate_eth_address(cls, value):
        return Web3.to_checksum_address(value)

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
