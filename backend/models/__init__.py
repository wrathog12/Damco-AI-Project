"""
ORM models.

Every model module must be imported here. Alembic's `env.py` imports this
package to populate `Base.metadata`, so a model that isn't re-exported below
is invisible to autogenerate and will silently never get a migration.
"""
from db.base import Base
from models.identity import User
from models.scheme import Scheme, SchemeEmbeddingState, SchemeTranslation

__all__ = ["Base", "Scheme", "SchemeTranslation", "SchemeEmbeddingState", "User"]
