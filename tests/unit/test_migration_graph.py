from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_migration_graph_has_single_head() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config(repo_root / "alembic.ini")
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()

    assert len(heads) == 1, f"expected one Alembic head, found: {', '.join(heads)}"
