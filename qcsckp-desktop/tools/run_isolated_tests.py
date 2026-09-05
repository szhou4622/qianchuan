"""Run regression tests without production data or external network access.

Usage: python tools/run_isolated_tests.py [test_module[.TestCase.test_name] ...]
Without arguments, discover the full tests/ suite. Loopback test servers remain
available; real API traffic is blocked at the socket boundary.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch


def _local_destination(address):
    if not isinstance(address, tuple):
        return False
    host = str(address[0]).split("%")[0]
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path[:0] = [str(root), str(root / "tests")]
    sys.dont_write_bytecode = True
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        if not _local_destination(address):
            raise AssertionError("External network is forbidden in isolated tests")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _local_destination(address):
            raise AssertionError("External network is forbidden in isolated tests")
        return original_connect_ex(sock, address)

    with tempfile.TemporaryDirectory(prefix="qcsckp-regression-") as directory:
        environment = {
            "QCSCKP_HOME": directory,
            "QCSCKP_DATA_DIR": str(Path(directory) / "data"),
            "QCSCKP_ALLOW_LIVE_API_WRITES": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with patch.dict(os.environ, environment), patch.object(
            socket.socket, "connect", guarded_connect
        ), patch.object(socket.socket, "connect_ex", guarded_connect_ex):
            try:
                loader = unittest.defaultTestLoader
                suite = (unittest.TestSuite(loader.loadTestsFromName(name) for name in sys.argv[1:])
                         if sys.argv[1:] else loader.discover(str(root / "tests")))
                # Preserve file diagnostics, avoid flooding the console with
                # schema setup INFO logs. Tests can still create their own sinks.
                for handler in logging.getLogger("QianChuanPMCServices").handlers:
                    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                        handler.setLevel(logging.ERROR)
                result = unittest.TextTestRunner(verbosity=1).run(suite)
                return 0 if result.wasSuccessful() else 1
            finally:
                # Windows keeps log files open until handlers are closed.
                logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
