"""Compatibility entrypoint for the verified channel updater."""
import hashlib
from pathlib import Path
from services.channel_update import run_update


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_desktop_update(download_url: str, expected_sha256: str = "") -> dict:
    return run_update(download_url, expected_sha256)
