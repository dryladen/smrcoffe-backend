import hashlib
import json
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"


def load_fixture_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_fixture_payload(relative_path: str) -> Any:
    path = FIXTURE_ROOT / relative_path
    return json.loads(path.read_text(encoding="utf-8"))


def list_case_categories() -> list[str]:
    manifest = load_fixture_manifest()
    return [entry["category"] for entry in manifest["fixtures"]]


def build_fixture_hashes() -> dict[str, str]:
    manifest = load_fixture_manifest()
    relative_paths = [entry["path"] for entry in manifest["fixtures"]]
    relative_paths.extend(manifest["same_day_multi_file"]["files"])

    hashes: dict[str, str] = {}
    for relative_path in relative_paths:
        path = FIXTURE_ROOT / relative_path
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes[relative_path] = digest
    return hashes
