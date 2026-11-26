# PyInstaller spec: one-folder, windowed build -> <distpath>\mc-tracker\mc-tracker.exe
# Build with packaging\build.ps1 (pins PyInstaller, warms the OCR models, writes to D:\Maple-dist).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
ICON = SRC / "mctracker" / "res" / "icon.ico"

datas = [(str(SRC / "mctracker" / "res"), "mctracker/res")]  # svg + icon, found via Path(__file__).parent
datas.append((str(ROOT / "packaging" / "install.ps1"), "."))  # swaps in a staged build (follow.start_install)
binaries = []
hiddenimports = ["PySide6.QtSvg", "PySide6.QtNetwork"]  # qsvg plugin (QSS chevron), QtNetwork used by the app

# Packages with data files / native libs PyInstaller can't see from imports alone:
#   rapidocr      -> ONNX models (rapidocr/models/*.onnx), config.yaml, default_models.yaml
#   onnxruntime   -> onnxruntime*.dll, providers
#   windows_capture -> native .pyd/.dll
for pkg in ("rapidocr", "onnxruntime", "windows_capture"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "pytest", "IPython", "notebook", "scipy", "pandas",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtMultimedia",
    ],
    noarchive=False,
)
# Unused bulk: OpenCV's video codec DLL (we never open video) and Qt's software OpenGL (widgets only).
_DROP = ("opencv_videoio_ffmpeg", "opengl32sw")
a.binaries = [b for b in a.binaries if not any(x in b[0].lower() for x in _DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mc-tracker",
    icon=str(ICON),
    console=False,  # windowed: no terminal
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="mc-tracker", upx=False)
