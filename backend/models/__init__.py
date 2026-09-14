"""
ORM models.

Every model module must be imported here. Alembic's `env.py` imports this
package to populate `Base.metadata`, so a model that isn't re-exported below
is invisible to autogenerate and will silently never get a migration.
"""
from db.base import Base
from models.auth import OtpChallenge, RefreshToken
from models.identity import User
from models.profile import (Conversation, EligibilityEvaluation, Message,
                            SchemeInteraction, UserProfile)
from models.scheme import Scheme, SchemeEmbeddingState, SchemeTranslation

__all__ = [
    "Base",
    "Conversation",
    "EligibilityEvaluation",
    "Message",
    "OtpChallenge",
    "RefreshToken",
    "Scheme",
    "SchemeInteraction",
    "SchemeTranslation",
    "SchemeEmbeddingState",
    "User",
    "UserProfile",
]
