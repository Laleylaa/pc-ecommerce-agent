"""Project-anchored evaluation inputs and generated artifacts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
RESULTS = ROOT / "artifacts/evals"


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path
