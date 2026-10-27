from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health(
    session: AsyncSession = Depends(get_session),
):
    checks: dict[str, str] = {}

    try:
        await session.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # pragma: no cover - used for runtime diagnostics
        checks["postgres"] = f"error: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
    }
