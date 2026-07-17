"""Write the deterministic FastAPI schema used by the CI consistency check."""

from __future__ import annotations

import json
from pathlib import Path

from api.main import app

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def main() -> None:
    schema = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True)
    OUTPUT_PATH.write_text(f"{schema}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
