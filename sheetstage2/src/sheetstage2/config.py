from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("/Volumes/mini_plus/sheetstage2")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_id: str = "m-a-p/SheetSage2"

    @classmethod
    def from_environment(cls) -> Settings:
        configured = os.environ.get("SHEETSTAGE2_DATA_DIR")
        return cls(data_dir=Path(configured).expanduser() if configured else DEFAULT_DATA_DIR)

    @property
    def model_cache_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    def prepare(self) -> None:
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
