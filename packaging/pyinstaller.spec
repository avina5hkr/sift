# PyInstaller recipe for Sift.app (spec section 14). Run through packaging/build_dmg.sh, which
# sets SIFT_ROOT, SIFT_VERSION and (optionally) SIFT_STARTER_TEACHING.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(os.environ["SIFT_ROOT"])
VERSION = os.environ.get("SIFT_VERSION", "0.0.0")
STARTER = os.environ.get("SIFT_STARTER_TEACHING", "")

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "src" / "sift" / "resources"), "sift/resources"),
]
if STARTER:  # the owner's teaching, bundled into this build only (never committed)
    datas.append((STARTER, "sift/resources/teaching"))
datas += collect_data_files("onnxruntime")
datas += collect_data_files("webview")

binaries = []
for pkg in ("onnxruntime", "pillow_heif", "tokenizers"):
    binaries += collect_dynamic_libs(pkg)

hiddenimports = [
    "Vision",
    "AVFoundation",
    "CoreMedia",
    "Quartz",
    "UserNotifications",
    "webview.platforms.cocoa",
    "pillow_heif",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
]
hiddenimports += collect_submodules("sift")

excludes = [
    "mypy", "hypothesis", "pytest", "_pytest", "setuptools", "pip", "ruff", "IPython",
    "tkinter", "webview.platforms.winforms", "webview.platforms.gtk", "webview.platforms.qt",
    "webview.platforms.cef", "webview.platforms.android", "webview.platforms.edgechromium",
]

a = Analysis(
    [str(ROOT / "src" / "sift" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sift",
    console=False,
    target_arch="arm64",
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Sift")
app = BUNDLE(
    coll,
    name="Sift.app",
    bundle_identifier="local.sift.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "Sift",
        "CFBundleDisplayName": "Sift",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "14.0",  # numpy and onnxruntime wheels need 14
        "NSHighResolutionCapable": True,
        "NSRemovableVolumesUsageDescription": "Sift saves backups to your external drive.",
        "LSApplicationCategoryType": "public.app-category.utilities",
    },
)
