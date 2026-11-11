"""SDK modules are loaded from PYZ; external resources remain available."""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("baseopensdk", include_py_files=False)
