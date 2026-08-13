from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from voucherbot.api.rate_limit import health_rate_limit
from voucherbot.database.connection import get_session

router = APIRouter(tags=["health"])


@router.head("/health", dependencies=[Depends(health_rate_limit)])
@router.get("/health", dependencies=[Depends(health_rate_limit)])
async def health_check(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT count(*) FROM sources"))
    return {"status": "ok"}
