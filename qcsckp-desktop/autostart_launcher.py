#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发版Windows开机自启入口：先恢复运行环境，再加载GUI。"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--api-base-url", default="")
    parser.add_argument("--test-mode", action="store_true")
    args, _unknown = parser.parse_known_args()
    if args.data_dir:
        os.environ["QCSCKP_DATA_DIR"] = os.path.abspath(args.data_dir)
    if args.api_base_url:
        os.environ["QCSCKP_API_BASE_URL"] = args.api_base_url
    if args.test_mode:
        os.environ["QCSCKP_TEST_MODE"] = "1"

    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "gui_app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
