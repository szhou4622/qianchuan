"""
macOS 打包应用 ZIP 更新：下载 zip → 解压 → 将新包安装到「当前正在运行的 .app」路径。

安装路径与包名始终与当前进程一致（dest_bundle），与 zip 内 .app 文件名无关；
shutil.move(解压出的路径, dest_bundle) 会把新内容放到当前名称下，避免变成解压包里的名字。

流程：下载 → unzip → rename 当前包为 .old_<ts> → move 新包到 dest_bundle → xattr/chmod
→ 后台等进程退出 → 再 chmod/xattr → open → 清理 temp。

说明：开发模式（非冻结）拒绝。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict
from urllib.error import URLError
from urllib.request import Request, urlopen

from config import DATA_TEMP_DIR


def _darwin_app_bundle_path(exe_path: Path) -> Path | None:
    """.../Foo.app/Contents/MacOS/foo -> .../Foo.app"""
    try:
        if exe_path.parent.name == "MacOS" and exe_path.parent.parent.name == "Contents":
            return exe_path.parent.parent.parent
    except (OSError, ValueError):
        pass
    return None


def _find_new_app_in_extract(extract_root: Path) -> Path | None:
    """与 MindHear 一致：优先根目录 *.app，否则 rglob 第一个 .app（解压包内名字可与当前安装名不同）。"""
    found = next(extract_root.glob("*.app"), None)
    if found is not None:
        return found
    try:
        return next(extract_root.rglob("*.app"))
    except StopIteration:
        return None


def _apply_bundle_permissions(bundle: Path, macos_exe_basename: str) -> None:
    subprocess.run(["/bin/chmod", "-R", "755", str(bundle)], check=False)
    main_exe = bundle / "Contents" / "MacOS" / macos_exe_basename
    if main_exe.exists():
        try:
            os.chmod(main_exe, 0o755)
        except OSError:
            pass
    subprocess.run(["/usr/bin/xattr", "-cr", str(bundle)], check=False)


def run_desktop_update(download_url: str) -> Dict[str, Any]:
    """
    执行更新：成功启动后台 shell 后返回重启标记，由运行主管优雅退出。
    失败时返回 {"success": False, "message": "..."}。
    """
    if sys.platform != "darwin":
        return {"success": False, "message": "仅支持 macOS 环境更新"}

    if not getattr(sys, "frozen", False):
        return {"success": False, "message": "开发模式（非打包应用）不支持在线更新"}

    url = (download_url or "").strip()
    if not url:
        return {"success": False, "message": "缺少下载地址"}

    exe_path = Path(sys.executable).resolve()
    current_bundle = _darwin_app_bundle_path(exe_path)
    if current_bundle is None:
        return {
            "success": False,
            "message": "无法解析当前 .app 包路径（需为 …/应用.app/Contents/MacOS/可执行文件）",
        }

    # 必须以「当前正在使用的包路径」为安装目标，保证包名与 ~/.qcsckp/<hash>/ 等逻辑不变
    dest_bundle = current_bundle.resolve()
    macos_exe_name = exe_path.name

    data_temp = Path(DATA_TEMP_DIR)
    data_temp.mkdir(parents=True, exist_ok=True)
    zip_path = data_temp / "update_package.zip"
    extract_dir = data_temp / "update_extract"

    try:
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)

        req = Request(url, headers={"User-Agent": "QianchuanMaterialDesktop/1.0"})
        with urlopen(req, timeout=600) as resp:
            with open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out)

        subprocess.run(
            ["/usr/bin/unzip", "-o", "-q", str(zip_path), "-d", str(extract_dir)],
            check=True,
        )
        if (extract_dir / "__MACOSX").exists():
            shutil.rmtree(extract_dir / "__MACOSX", ignore_errors=True)

        new_app = _find_new_app_in_extract(extract_dir)
        if new_app is None:
            return {"success": False, "message": "更新包中未找到 .app"}

        ts = int(time.time())
        pid = os.getpid()
        old_bundle = dest_bundle.parent / f"{dest_bundle.name}.old_{ts}"

        try:
            os.rename(dest_bundle, old_bundle)
        except OSError as e:
            return {"success": False, "message": f"无法移走当前应用（可能被占用）: {e}"}

        try:
            # 目标必须是 dest_bundle（当前包名/路径），不要用 new_app.name
            shutil.move(str(new_app.resolve()), str(dest_bundle))
        except OSError as e:
            try:
                if dest_bundle.exists():
                    shutil.rmtree(dest_bundle, ignore_errors=True)
                os.rename(old_bundle, dest_bundle)
            except OSError:
                pass
            return {"success": False, "message": f"安装新包失败: {e}"}

        _apply_bundle_permissions(dest_bundle, macos_exe_name)

        q_dest = shlex.quote(str(dest_bundle))
        q_zip = shlex.quote(str(zip_path.resolve()))
        q_extract = shlex.quote(str(extract_dir.resolve()))
        q_old = shlex.quote(str(old_bundle))

        cleanup_cmd = "; ".join(
            [
                f"rm -rf {q_extract} 2>/dev/null || true",
                f"rm -rf {q_old} 2>/dev/null || true",
                f"rm -f {q_zip} 2>/dev/null || true",
            ]
        )

        restart_cmd = (
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.1; done; "
            f"chmod -R 755 {q_dest}; "
            f"/usr/bin/xattr -cr {q_dest}; "
            f"sleep 0.5; "
            f"/usr/bin/open -n {q_dest} || true; "
            f"{cleanup_cmd}"
        )

        subprocess.Popen(
            ["/bin/sh", "-c", restart_cmd],
            cwd=str(data_temp),
            start_new_session=True,
            close_fds=True,
        )
        return {
            "success": True,
            "message": "更新已准备，工具正在安全退出并安装新版本",
            "restart_scheduled": True,
        }
    except subprocess.CalledProcessError:
        return {"success": False, "message": "更新包解压失败（unzip）"}
    except URLError as e:
        return {"success": False, "message": f"下载失败: {e.reason}"}
    except OSError as e:
        return {"success": False, "message": f"文件操作失败: {e}"}
    except Exception as e:
        return {"success": False, "message": str(e)}
