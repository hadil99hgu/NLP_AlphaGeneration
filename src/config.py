from datetime import date, timedelta
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    """Load a YAML configuration file."""

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:
        return yaml.safe_load(file)


def resolve_end_date(
    value: str | None,
) -> str:
    """Use tomorrow as the exclusive end date when no date is specified."""

    if value is None:
        return (
            date.today() + timedelta(days=1)
        ).isoformat()

    return value


def resolve_path(
    relative_path: str,
) -> Path:
    """Resolve a path relative to the project root."""

    return PROJECT_ROOT / relative_path