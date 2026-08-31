"""Verified same-channel staging; the installer waits for graceful exit."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from release_identity import CHANNEL


def validate_payload(payload, channel=CHANNEL):
    payload = Path(payload)
    manifest = json.loads((payload / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    if manifest.get("app_name") != "QCSCKP" or manifest.get("channel") != channel:
        raise ValueError("安装包渠道不匹配，已停止更新")
    for item in manifest.get("critical_files", []):
        path = (payload / item["path"]).resolve()
        if not path.is_relative_to(payload.resolve()) or not path.is_file():
            raise ValueError("安装包文件路径或完整性无效")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda:f.read(1024*1024), b""):
                h.update(block)
        if path.stat().st_size != item["size"] or h.hexdigest() != item["sha256"]:
            raise ValueError("安装包关键文件校验失败")
    required={"QCSCKP.exe","bin/python312.dll","bin/release.json","bin/static/index.html","bin/static/license.html"}
    if not required.issubset({x["path"] for x in manifest.get("critical_files", [])}):
        raise ValueError("安装包缺少必要校验项")
    identity=json.loads((payload/"bin/release.json").read_text(encoding="utf-8-sig"))
    for key in ("app_name","channel","version","build_revision"):
        if identity.get(key)!=manifest.get(key):
            raise ValueError("安装包身份信息不一致")
    return manifest


def safe_extract(archive, target):
    target=Path(target).resolve()
    with zipfile.ZipFile(archive) as z:
        total=sum(i.file_size for i in z.infolist())
        if total>3*1024**3 or shutil.disk_usage(target.parent).free<total*2+64*1024**2:
            raise ValueError("安装包过大或空间不足")
        for item in z.infolist():
            dest=(target/item.filename.replace("\\","/")).resolve()
            if not dest.is_relative_to(target) or ((item.external_attr>>16)&0o170000)==0o120000:
                raise ValueError("安装包包含越界路径或链接")
        z.extractall(target)
    roots=[target]+[p for p in target.iterdir() if p.is_dir()]
    candidates=[p for p in roots if (p/"QCSCKP.exe").is_file() and (p/"bin").is_dir()]
    if len(candidates)!=1:
        raise ValueError("安装包结构无效")
    return candidates[0]


def run_update(download_url, expected_sha256):
    if sys.platform!="win32" or not getattr(sys,"frozen",False):
        return {"success":False,"message":"仅打包后的 Windows 软件支持在线更新"}
    if CHANNEL=="stable":
        return {"success":False,"message":"历史稳定版已冻结；换版请使用作者提供的独立安装包"}
    parsed=urlparse(download_url)
    if parsed.scheme!="https" or parsed.hostname!="update.dadaozixun.com" or len(expected_sha256)!=64:
        return {"success":False,"message":"更新地址或校验值无效"}
    root=Path(sys.executable).resolve().parent
    if not (root/"bin").is_dir() or Path(sys.executable).name!="QCSCKP.exe":
        return {"success":False,"message":"程序目录身份校验失败"}
    stage=root/".qcsckp-update"/uuid.uuid4().hex
    try:
        stage.mkdir(parents=True,exist_ok=False)
        archive=stage/"package.zip"
        h=hashlib.sha256(); size=0
        with urlopen(Request(download_url,headers={"User-Agent":"QCSCKP-Channel-Updater"}),timeout=120) as response:
            if urlparse(response.geturl()).hostname!="update.dadaozixun.com":
                raise ValueError("更新下载发生非官方跳转")
            with archive.open("xb") as out:
                for block in iter(lambda:response.read(1024*1024),b""):
                    size+=len(block)
                    if size>1024**3: raise ValueError("更新包超过大小限制")
                    h.update(block); out.write(block)
        if h.hexdigest()!=expected_sha256.lower():
            raise ValueError("安装包 SHA256 校验失败")
        payload=safe_extract(archive,stage/"unpacked")
        validate_payload(payload)
        helper=Path(sys._MEIPASS)/"apply_channel_update.ps1"
        shutil.copy2(helper,stage/"apply.ps1")
        context={"root":str(root),"payload":str(payload),"stage":str(stage),"old_pid":os.getpid()}
        (stage/"context.json").write_text(json.dumps(context),encoding="utf-8")
        subprocess.Popen(["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
            "-File",str(stage/"apply.ps1"),"-ContextFile",str(stage/"context.json")],
            creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        return {"success":True,"message":"安装包已校验，正在安全退出后更新；旧版备份将保留", "restart_scheduled":True}
    except Exception as exc:
        from services.diagnostics import record_event
        record_event("update","runtime_failure",exception=exc)
        return {"success":False,"message":"更新未完成，原程序未替换："+str(exc)}
