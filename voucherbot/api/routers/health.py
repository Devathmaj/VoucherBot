from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.head("/health")
@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
