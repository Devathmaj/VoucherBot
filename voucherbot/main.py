import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI
import structlog

from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import or_, update

from voucherbot.api.routers import health
from voucherbot.config.settings import settings
from voucherbot.core.logging import setup_logging
from voucherbot.database.bootstrap import bootstrap_data
from voucherbot.database.connection import session_scope
from voucherbot.models.source import Source
from voucherbot.services.dispatcher import reset_lease, set_process_boot_at
from voucherbot.services.email.sender import send_test_email
from voucherbot.services.scheduler import start_scheduler, stop_scheduler

logger = structlog.get_logger()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_migrations() -> None:
    """Run `alembic upgrade head` against the configured database."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # env.py only calls fileConfig() when config_file_name is set; suppress it
    # so alembic.ini's logging config doesn't clobber the app's structlog setup.
    cfg.config_file_name = None
    alembic_command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging()
    set_process_boot_at()
    await logger.ainfo("Starting up VoucherBot API...", is_prod=settings.is_prod)
    # Non-prod: apply migrations (schema ownership) then seed. Production:
    # schema is applied ahead of time and the admin bootstrap is manual, so
    # skip DB setup entirely.
    if not settings.is_prod:
        await logger.ainfo("Applying alembic migrations")
        await asyncio.to_thread(run_migrations)
        await bootstrap_data()
    else:
        await logger.ainfo("Skipping DB init/bootstrap (IS_PROD=true)")

    async with session_scope() as session:
        await session.execute(
            update(Source)
            .where(
                or_(
                    Source.next_due_at.is_not(None),
                    Source.backoff_until.is_not(None),
                )
            )
            .values(next_due_at=None, backoff_until=None)
        )
        await session.commit()
    await logger.ainfo("scheduler: all sources reset to due")

    async with session_scope() as session:
        await reset_lease(session)
    await logger.ainfo("dispatcher: lease reset on startup")

    start_scheduler()

    # Send a test email to verify the Resend configuration on startup
    await send_test_email()

    yield
    await stop_scheduler()
    await logger.ainfo("Shutting down VoucherBot API...")


app = FastAPI(
    title="VoucherBot API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
