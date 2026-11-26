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
# Price-check popup embeds maplestory.watch (price_popup.py imports these lazily, so name them here).
hiddenimports += ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"]

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
        # QtWebEngineCore links Qt6Quick/Qt6Qml DLLs (found as binary deps), but the Python modules'
        # hooks would collect the whole QML tree (+240 MB of plugins, measured): keep them out.
        "PySide6.QtWebEngineQuick", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickWidgets",
        "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtMultimedia",
    ],
    noarchive=False,
)
# Unused bulk: OpenCV's video codec DLL (we never open video) and Qt's software OpenGL (opengl32sw): the
# widgets don't use OpenGL, and QtWebEngine's Chromium renders through ANGLE/D3D, falling back to its own
# built-in software renderer, not this DLL.
_DROP = ("opencv_videoio_ffmpeg", "opengl32sw")
a.binaries = [b for b in a.binaries if not any(x in b[0].lower() for x in _DROP)]
# QtWebEngine extras the price popup never uses: DevTools and debug resources, and every UI locale but
# en-US (Chromium falls back to en-US; the site's own language is unaffected).
_WEB_DROP = ("devtools_resources", ".debug.pak", ".debug.bin")


def _keep_data(dest: str) -> bool:
    d = dest.replace("\\", "/").lower()
    if any(x in d for x in _WEB_DROP):
        return False
    return "qtwebengine_locales/" not in d or d.endswith("/en-us.pak")


a.datas = [x for x in a.datas if _keep_data(x[0])]

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
