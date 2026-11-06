# PyInstaller spec for LaunchDeck (one-folder build: starts faster than one-file).
# Build with:  build_exe.bat   (or: python -m PyInstaller --noconfirm --clean launchdeck.spec)

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=["PySide6.QtSvg", "PySide6.QtNetwork"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LaunchDeck",
    icon="assets/launchdeck.ico",
    console=False,
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="LaunchDeck")
