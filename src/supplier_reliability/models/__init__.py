"""ORM models. Import every model module so Base.metadata is complete."""

from supplier_reliability.models import master  # noqa: F401
from supplier_reliability.models.base import Base

__all__ = ["Base"]
