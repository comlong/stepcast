# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置。不要直接运行，用：build_exe.bat（或 python packaging/build_exe.py）
#
# 打出来的是一个文件夹（onedir）：StepCast.exe + _internal（所有库）。
# 不用单文件 exe：这个项目的依赖有几百 MB，单文件每次启动都要先解压到临时目录，慢且容易被杀毒软件误报。
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
    "arabic_reshaper", "bidi",                        # 阿拉伯 / 希伯来文排版（非 Windows 兜底）
]
# fontTools 读字体表时按表名动态导入模块（_c_m_a_p、_n_a_m_e、O_S_2f_2……），静态分析找不到
hiddenimports += collect_submodules("fontTools.ttLib.tables")

# 这些包带数据文件 / DLL / 运行时才导入的子模块，整包收进来
for pkg in (
    "faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers", "huggingface_hub",  # 语音识别
    "imageio_ffmpeg",                                                                      # 自带 ffmpeg
    "edge_tts", "certifi", "pyttsx3", "comtypes",                                          # 配音
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
    console=True,              # 保留命令行窗口：能看到地址和报错，关掉窗口就停止服务
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
