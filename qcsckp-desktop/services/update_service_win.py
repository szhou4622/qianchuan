"""
Windows 打包应用 ZIP 覆盖更新（参考 MindHear update 流程简化版）。

流程：下载 zip → 解压 → 校验包内须同时含「根级 .exe」与「bin 目录」→ 生成批处理：
结束当前进程 → 将旧 exe / bin 备份到 data\\temp → 复制新文件 → 启动新 exe → 清理 temp（含备份、zip、解压目录）。

说明：开发模式（非 PyInstaller 冻结）直接拒绝，避免误伤 Python 环境。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen
from config import PROJECT_ROOT, DATA_TEMP_DIR

def _open_zip_read(path: Path) -> zipfile.ZipFile:
    """
    打开待解压的 zip。中文目录/软件名在 zip 里可能是 UTF-8（标准扩展）或 GBK（国内工具常见）；
    若 metadata_encoding=\"utf-8\" 解析中央目录失败（UnicodeDecodeError），再试 gbk，最后退回旧版解析。
    """
    try:
        return zipfile.ZipFile(path, "r", metadata_encoding="utf-8")
    except TypeError:
        # Python < 3.11 无 metadata_encoding
        return zipfile.ZipFile(path, "r")
    except UnicodeDecodeError:
        try:
            return zipfile.ZipFile(path, "r", metadata_encoding="gbk")
        except (UnicodeDecodeError, LookupError, OSError):
            return zipfile.ZipFile(path, "r")




def _find_payload_with_exe_and_bin(extract_root: Path) -> Tuple[Optional[Path], str]:
    """
    在解压目录下查找同时包含「至少一个根级 .exe」与「bin 子目录」的目录。
    支持 zip 根目录即内容，或仅一层子目录包裹。
    """
    candidates: List[Path] = [extract_root]
    try:
        subs = [x for x in extract_root.iterdir() if x.is_dir()]
    except OSError:
        subs = []
    if len(subs) == 1 and not (extract_root / "bin").is_dir():
        candidates.append(subs[0])

    for base in candidates:
        bin_dir = base / "bin"
        if not bin_dir.is_dir():
            continue
        exes = [x for x in base.iterdir() if x.is_file() and x.suffix.lower() == ".exe"]
        if exes:
            return base, ""
    return None, "更新包中未找到同时包含「可执行文件 .exe」与「bin 目录」的结构"


def _pick_new_exe(payload: Path, target_exe_name: str) -> Optional[Path]:
    exes = [x for x in payload.iterdir() if x.is_file() and x.suffix.lower() == ".exe"]
    if not exes:
        return None
    t = target_exe_name.lower()
    for e in exes:
        if e.name.lower() == t:
            return e
    return exes[0]


def run_desktop_update(download_url: str) -> Dict[str, Any]:
    """
    执行更新：成功启动批处理并退出进程时不返回（os._exit）。
    失败时返回 {"success": False, "message": "..."}。
    """
    if sys.platform != "win32":
        return {"success": False, "message": "仅支持 Windows 环境更新"}

    if not getattr(sys, "frozen", False):
        return {"success": False, "message": "开发模式（非打包 exe）不支持在线更新"}

    url = (download_url or "").strip()
    if not url:
        return {"success": False, "message": "缺少下载地址"}

    exe_path = Path(sys.executable).resolve()
    exe_name = exe_path.name
    root = Path(PROJECT_ROOT)

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

        with _open_zip_read(zip_path) as zf:
            zf.extractall(extract_dir)

        payload, err = _find_payload_with_exe_and_bin(extract_dir)
        if not payload:
            return {"success": False, "message": err or "无效的更新包"}

        new_exe = _pick_new_exe(payload, exe_name)
        new_bin = payload / "bin"
        if not new_exe or not new_bin.is_dir():
            return {"success": False, "message": "更新包中缺少 exe 或 bin 目录"}

        ts = int(time.time())
        pid = os.getpid()
        root_s = str(root.resolve()).replace("/", "\\")
        temp_s = str(data_temp.resolve()).replace("/", "\\")
        payload_s = str(payload.resolve()).replace("/", "\\")
        zip_s = str(zip_path.resolve()).replace("/", "\\")
        extract_s = str(extract_dir.resolve()).replace("/", "\\")
        new_exe_name = new_exe.name

        bak_exe = f"{exe_path.stem}_bak_{ts}.exe"
        bak_bin = f"bin_bak_{ts}"
        bak_exe_path = f"{temp_s}\\{bak_exe}"
        bak_bin_path = f"{temp_s}\\{bak_bin}"

        # 批处理须与 chcp 65001 一致使用 UTF-8（含 BOM），否则含中文的 payload 路径在 GBK 与 UTF-8 混用时会在 xcopy/copy 中乱码。
        bat_lines = [
            "@echo off",
            "chcp 65001 >nul",
            f'cd /d "{root_s}"',
            "echo [更新] 结束当前进程...",
            f"taskkill /F /PID {pid} >nul 2>&1",
            "timeout /t 2 /nobreak >nul",
            "echo [更新] 备份并替换核心文件...",
            f'if exist "{exe_name}" (',
            f'  if exist "{bak_exe_path}" del /f /q "{bak_exe_path}"',
            f'  move /y "{exe_name}" "{bak_exe_path}"',
            ")",
            f'if exist "bin" (',
            f'  if exist "{bak_bin_path}" rd /s /q "{bak_bin_path}"',
            f'  move /y "bin" "{bak_bin_path}"',
            ")",
            f'copy /y "{payload_s}\\{new_exe_name}" "{exe_name}"',
            f'if not exist "bin" mkdir "bin"',
            f'xcopy /e /i /y "{payload_s}\\bin\\*" "bin\\"',
            "echo [更新] 启动新版本...",
            f'start "" "{root_s}\\{exe_name}"',
            "timeout /t 1 /nobreak >nul",
            "echo [更新] 清理临时文件...",
            f'rd /s /q "{extract_s}" 2>nul',
            f'del /f /q "{bak_exe_path}" 2>nul',
            f'rd /s /q "{bak_bin_path}" 2>nul',
            f'del /f /q "{zip_s}" 2>nul',
            r'del /f /q "%~f0" 2>nul',
        ]
        bat_path = data_temp / f"qc_apply_update_{ts}.bat"
        bat_path.write_text("\r\n".join(bat_lines), encoding="utf-8-sig")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            cwd=str(root),
            creationflags=creationflags,
            close_fds=True,
        )
        time.sleep(0.4)
        os._exit(0)
    except zipfile.BadZipFile:
        return {"success": False, "message": "更新包不是有效的 ZIP 文件"}
    except UnicodeDecodeError as e:
        return {
            "success": False,
            "message": f"ZIP 内路径编码无法解析（请用 UTF-8 或常见中文工具重新打包）: {e}",
        }
    except URLError as e:
        return {"success": False, "message": f"下载失败: {e.reason}"}
    except OSError as e:
        return {"success": False, "message": f"文件操作失败: {e}"}
    except Exception as e:
        return {"success": False, "message": str(e)}
