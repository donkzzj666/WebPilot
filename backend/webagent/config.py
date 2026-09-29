"""Local bootstrap configuration, not the M1-07 model/config snapshot service."""

from dataclasses import dataclass
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def disable_external_tracing() -> None:
    # Override inherited V2 flags too; these can take precedence over TRACING.
    for key in (
        "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING",
        "LANGCHAIN_TRACING_V2", "LANGCHAIN_HANDLER",
    ):
        os.environ[key] = "false"


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.environ.get("WEBAGENT_DATA_DIR")
        path = Path(raw).expanduser() if raw else PROJECT_ROOT / "data"
        if not path.is_absolute():
            raise ValueError("WEBAGENT_DATA_DIR must be an absolute local directory")
        return cls(data_dir=path.resolve())

    @property
    def business_db(self) -> Path:
        return self.data_dir / "business.sqlite3"

    @property
    def graph_db(self) -> Path:
        return self.data_dir / "graph.sqlite3"

