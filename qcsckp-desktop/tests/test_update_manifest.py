import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from config import APP_NAME
from services.update_manifest import check_for_update
from services.update_service_win import _sha256_file


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class UpdateManifestTests(unittest.TestCase):
    def test_selects_windows_artifact_and_checksum(self):
        checksum = "a" * 64
        payload = {
            "app_name": APP_NAME,
            "version": "0.1.58",
            "min_supported_version": "0.1.57",
            "download_url": {
                "windows_x64": "https://update.dadaozixun.com/apps/QCSCKP/0.1.58/windows.zip"
            },
            "sha256": {"windows_x64": checksum},
            "notes": ["test"],
            "force": False,
        }
        result = check_for_update(
            "0.1.57",
            opener=lambda _request, timeout=None: _Response(payload),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["has_update"])
        self.assertEqual(checksum, result["data"]["sha256"])

    def test_missing_manifest_is_not_reported_as_update_failure(self):
        def missing(_request, timeout=None):
            raise HTTPError("https://update.example", 404, "missing", {}, io.BytesIO(b"{}"))

        result = check_for_update("0.1.57", opener=missing)
        self.assertTrue(result["success"])
        self.assertFalse(result["data"]["has_update"])

    def test_non_official_artifact_is_rejected(self):
        payload = {
            "app_name": APP_NAME,
            "version": "0.1.58",
            "download_url": {"windows_x64": "https://evil.example/update.zip"},
            "sha256": {"windows_x64": "b" * 64},
            "notes": [],
            "force": False,
            "min_supported_version": "0.1.57",
        }
        with self.assertRaises(ValueError):
            check_for_update(
                "0.1.57",
                opener=lambda _request, timeout=None: _Response(payload),
            )

    def test_streaming_sha256_matches_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "update.zip"
            path.write_bytes(b"qcsckp-update-payload")
            self.assertEqual(
                "81b3c0456ca8810f4829f6ae56d503f98238d00bfb5f480066dbc6f5da6bdf91",
                _sha256_file(path),
            )


if __name__ == "__main__":
    unittest.main()
