# Import all models here to ensure proper initialization order

# First import the base model
from src.models.base import Base

# Then import all models that don't have relationships to other models
from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus, CreditTransactionProvider
from src.models.inference_call import InferenceCall

# Then import models with relationships but no circular dependencies
from src.models.user import User
from src.models.api_key import ApiKey

# This ensures all models are loaded and SQLAlchemy can properly establish relationships
__all__ = [
    "Base",
    "User",
    "ApiKey",
    "CreditTransaction",
    "CreditTransactionStatus",
    "CreditTransactionProvider",
    "InferenceCall",
]
