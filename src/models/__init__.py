# Import all models here to ensure proper initialization order

# First import the base model
from src.models.base import Base

# Then import all models that don't have relationships to other models
from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus, CreditTransactionProvider
from src.models.inference_call import InferenceCall
from src.models.subscription_transaction import SubscriptionTransaction, SubscriptionTransactionStatus

# Then import models with relationships but no circular dependencies
from src.models.user import User
from src.models.api_key import ApiKey
from src.models.subscription import Subscription, SubscriptionType, SubscriptionStatus

# Finally import models with potential circular dependencies
from src.models.agent import Agent

# This ensures all models are loaded and SQLAlchemy can properly establish relationships
__all__ = [
    "Base",
    "User",
    "ApiKey",
    "CreditTransaction",
    "CreditTransactionStatus",
    "CreditTransactionProvider",
    "InferenceCall",
    "Subscription",
    "SubscriptionType",
    "SubscriptionStatus",
    "SubscriptionTransaction",
    "SubscriptionTransactionStatus",
    "Agent",
]
