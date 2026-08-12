from fastapi import APIRouter, Depends

from voucherbot.api.rate_limit import health_rate_limit

router = APIRouter(tags=["health"])


@router.head("/health", dependencies=[Depends(health_rate_limit)])
@router.get("/health", dependencies=[Depends(health_rate_limit)])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
