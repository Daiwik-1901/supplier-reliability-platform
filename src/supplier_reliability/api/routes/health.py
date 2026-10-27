from fastapi import APIRouter

from supplier_reliability import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only; does not touch the database."""
    return {"status": "ok", "version": __version__}
