# -*- mode: python ; coding: utf-8 -*-
# PyInstaller configuration. Don't run it directly; use build_exe.bat (or python packaging/build_exe.py)
#
# The result is a folder (onedir): StepCast.exe + _internal (all libraries).
# Not a single-file exe: the dependencies are several hundred MB, and a single file would unpack itself to a temp folder on every start — slow and prone to antivirus false positives.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

datas = [(os.path.join(ROOT, "static"), "static")]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules("backend")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "python_multipart", "multipart",
    "win32com.client", "pythoncom", "pywintypes",
    "pyttsx3.drivers", "pyttsx3.drivers.sapi5",
    "arabic_reshaper", "bidi",                        # Arabic / Hebrew layout (fallback outside Windows)
]
# fontTools imports modules dynamically by table name when reading fonts (_c_m_a_p, _n_a_m_e, O_S_2f_2 …); static analysis can't find them
hiddenimports += collect_submodules("fontTools.ttLib.tables")

# these packages ship data files / DLLs / submodules imported at run time; collect them completely
for pkg in (
    "faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers", "huggingface_hub",  # speech recognition
    "imageio_ffmpeg",                                                                      # bundled ffmpeg
    "edge_tts", "certifi", "pyttsx3", "comtypes",                                          # voice-over
    "pptx", "pymupdf",                                                                     # PPT / PDF
    "anthropic",                                                                           # Claude
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "torch", "tensorflow", "pandas", "scipy",
              "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StepCast",
    console=True,              # keep the console window: shows the address and errors; closing it stops the service
    icon=os.path.join(SPECPATH, "icon.ico"),
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StepCast",
)
