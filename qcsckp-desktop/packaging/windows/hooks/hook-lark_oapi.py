"""Keep the SDK modules, native dependencies and data; avoid duplicate sources."""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("lark_oapi", include_py_files=False)
