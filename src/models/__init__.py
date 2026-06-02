# Import all models here to ensure proper initialization order

# First import the base model
from src.models.base import Base

# Then import all models that don't have relationships to other models
from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus, CreditTransactionProvider
from src.models.inference_call import InferenceCall

# Then import models with relationships but no circular dependencies
from src.models.user import User
from src.models.api_key import ApiKey

# Identity / auth models
from src.models.wallet_connection import WalletConnection
from src.models.oauth_connection import OAuthConnection
from src.models.session import Session
from src.models.magic_link import MagicLink
from src.models.auth_code import AuthCode
from src.models.wallet_challenge import WalletChallenge

# This ensures all models are loaded and SQLAlchemy can properly establish relationships
__all__ = [
    "Base",
    "User",
    "ApiKey",
    "CreditTransaction",
    "CreditTransactionStatus",
    "CreditTransactionProvider",
    "InferenceCall",
    "WalletConnection",
    "OAuthConnection",
    "Session",
    "MagicLink",
    "AuthCode",
    "WalletChallenge",
]
