"""Locate the frozen contract data directory."""
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SCHEMA_DIR = DATA_DIR / "schemas"
FIXTURE_DIR = DATA_DIR / "fixtures"
PROFILE_DIR = DATA_DIR / "profiles"
REVISION_FILE = DATA_DIR / "REVISION"
