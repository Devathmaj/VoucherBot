"""Structural tests for the Alembic migration graph.

Guards the guarantee that ``alembic upgrade head`` reproduces the schema the
running models define: the chain must be a single linear path with exactly one
head, every ``down_revision`` must resolve, and every model table must be
registered in ``Base.metadata``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from voucherbot.models.base import Base

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"

_EXPECTED_HEAD = "m6n7o8p9q0r1"

# Every model-backed table (views are excluded — voucher_posts is created via
# migrations as a view, not through Base.metadata.create_all).
_EXPECTED_TABLES = {
    "sources",
    "posts",
    "events",
    "keywords",
    "vendor_mappings",
    "pipeline_lock",
    "notification_outbox",
}


def _load_revisions() -> dict[str, Any]:
    """Import every migration module and return {revision: module}."""
    revisions: dict[str, Any] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions[module.revision] = module
    assert revisions, "no migration files found"
    return revisions


def _import_all_models() -> None:
    import voucherbot.models.event  # noqa: F401
    import voucherbot.models.keyword  # noqa: F401
    import voucherbot.models.notification  # noqa: F401
    import voucherbot.models.pipeline_lock  # noqa: F401
    import voucherbot.models.post  # noqa: F401
    import voucherbot.models.source  # noqa: F401
    import voucherbot.models.vendor_mapping  # noqa: F401


def test_migration_chain_is_single_linear_path() -> None:
    revisions = _load_revisions()

    children_of: dict[str, int] = {rev: 0 for rev in revisions}
    for module in revisions.values():
        parent = module.down_revision
        if parent is not None:
            assert parent in revisions, f"unknown down_revision {parent!r}"
            children_of[parent] += 1

    heads = [rev for rev, n in children_of.items() if n == 0]
    assert heads == [_EXPECTED_HEAD], (
        f"expected single head {_EXPECTED_HEAD}, got {heads}"
    )
    branched = [rev for rev, n in children_of.items() if n > 1]
    assert not branched, f"branch in migration chain at {branched}"


def test_chain_starts_from_none() -> None:
    revisions = _load_revisions()
    roots = [rev for rev, m in revisions.items() if m.down_revision is None]
    assert len(roots) == 1, f"expected exactly one root revision, got {roots}"


def test_all_model_tables_registered() -> None:
    _import_all_models()
    actual = set(Base.metadata.tables)
    assert _EXPECTED_TABLES <= actual, _EXPECTED_TABLES - actual


def test_migration_head_exists() -> None:
    revisions = _load_revisions()
    assert _EXPECTED_HEAD in revisions
