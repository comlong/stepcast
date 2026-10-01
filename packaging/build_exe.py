"""Build the Windows version that can be handed to colleagues directly.

    build_exe.bat                 # recommended (uses the project's .venv)
    python packaging/build_exe.py

Output:
    dist/StepCast/                    double-click StepCast.exe to run
        StepCast.exe
        _internal/                    all libraries (Python, ffmpeg, speech recognition …), don't delete
        extension/                    Chrome extension; choose it under chrome://extensions → "Load unpacked"
        README.txt
    dist/StepCast-<version>-win64.zip     the zip to share

    build_exe.bat --with-model            also build a zip that includes the speech recognition model (small),
                                          for colleagues who can't download the model (mainland China / company firewalls); works offline after unzipping
    build_exe.bat --with-model medium     a different model size

Your own config.json (with API keys) and projects/ are never included.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
APP_DIR = DIST / "StepCast"

README = """StepCast
========

[English]

1. Double-click StepCast.exe. A console window opens (keep it open — closing it stops the app) and the editor opens in your browser.
   If Windows shows "Windows protected your PC", click "More info" → "Run anyway".
2. Install the Chrome extension (once):
   open chrome://extensions → enable "Developer mode" (top right) → "Load unpacked" → select the extension folder inside this folder.
3. In the editor, open ⚙ Settings (top right) to choose an AI provider and enter your API key. The language menu at the top right switches the interface language.

Where your data is stored:
- All projects (screenshots, voice-overs, rendered videos) are in the projects\\ folder next to the exe; videos are in projects\\<project id>\\output\\
- Uploaded PPT / PDF files go to projects\\_imports\\ and are removed automatically 48 hours after the project is created
- Settings (including API keys) are in config.json next to the exe
- If this folder is not writable (e.g. inside C:\\Program Files), the above are stored in %LOCALAPPDATA%\\StepCast instead
- The speech recognition model is downloaded on first use to %USERPROFILE%\\.cache\\huggingface (about 465 MB).
  If the official source (HuggingFace) is unreachable, the China mirror hf-mirror.com is used automatically.
  If it can't be downloaded at all (e.g. a company firewall), put the model folder in models\\faster-whisper-small\\
  inside this folder; the package with "with-model" in its name already contains it
- Videos saved with "Download MP4" in the editor go to your browser's download folder

Upgrading: unzip the new version, then copy the projects folder and config.json from the old folder into the new one.
Never share your own config.json — it contains your API keys.

Rules for use (please follow them):
1. Do not record screens that contain personal data of customers or colleagues. When narration is
   generated, the text of the element you clicked, what you typed into fields, the page title and the
   URL are sent to the AI provider you selected in Settings; blurring the picture does not affect this text.
   (Password fields are the exception: they are replaced with •••• while recording, and are never stored or sent.)
2. Do not send internal or confidential material to AI providers abroad. You can write the narration
   yourself, or switch to a locally running Ollama in Settings so that nothing leaves your computer.
3. The voice input feature turns on the microphone. If other people may be recorded, tell them first.

Disclaimer:
This software runs on your computer, but some features use online services from third-party companies
(AI-written narration, translation and rewriting, AI voice-over, downloading the speech recognition
model), and the related text is sent to those companies' servers. The availability, cost, data handling
and terms of use of these services are determined by the companies that provide them and are outside
the control of this software.
Before use, please make sure yourself that the content you process may be sent to external services and
that doing so complies with applicable laws and regulations (including personal data and data protection
rules), your company's policies and your confidentiality obligations. This software is provided as is,
without any warranty. You personally bear the legal risks, compliance responsibility and any other
consequences arising from its use.
(The same text is shown in the editor under ⚙ Settings → About.)

Third-party components and licenses:
This software includes the following open-source components, copyright of their respective authors:
- PyMuPDF (AGPL-3.0): reading PDF files
- ffmpeg (GPLv3): audio and video encoding
- edge-tts (LGPL-3.0), python-bidi (LGPL-3.0): online voice-over, right-to-left text layout
- pyttsx3 (MPL-2.0): offline voice-over fallback
- faster-whisper, CTranslate2, Pillow, FastAPI, pydantic, python-pptx, fontTools, onnxruntime,
  arabic-reshaper (MIT)
- uvicorn, numpy, PyAV (BSD); requests, huggingface-hub, aiohttp, python-multipart (Apache-2.0);
  pywin32 (PSF)
The source code and full license texts of these components are available on request from whoever
provided you with this software.
StepCast itself is released under the Apache License 2.0 (see LICENSE.txt); source code: https://github.com/comlong/stepcast

[中文]

1. 双击 StepCast.exe。会打开一个命令行窗口（不要关，关掉就停止了），浏览器会自动打开编辑器。
   第一次运行时 Windows 可能提示「Windows 已保护你的电脑」：点「更多信息」→「仍要运行」。
2. 安装 Chrome 扩展（只需一次）：
   Chrome 地址栏输入 chrome://extensions → 打开右上角「开发者模式」→「加载已解压的扩展程序」→ 选择本文件夹里的 extension 文件夹。
3. 在编辑器右上角 ⚙ 设置里选 AI 服务商、填 API Key。右上角下拉框可以切换界面语言。

数据保存在哪里：
- 所有项目（录制的截图、配音、生成的视频）在本文件夹的 projects\\ 里，视频在 projects\\<项目id>\\output\\
- 上传的 PPT / PDF 先放在 projects\\_imports\\，生成项目后 48 小时自动清理
- 设置（含 API Key）在本文件夹的 config.json
- 如果本文件夹没有写入权限（比如放在 C:\\Program Files），以上内容会改存到 %LOCALAPPDATA%\\StepCast
- 语音识别模型第一次使用时自动下载到 %USERPROFILE%\\.cache\\huggingface（约 465 MB）。
  官方源（HuggingFace）连不上时自动改用国内镜像 hf-mirror.com。
  完全下载不了（比如公司网络限制）：把模型文件夹放到本文件夹的 models\\faster-whisper-small\\，
  文件名里带 with-model 的压缩包已经放好了
- 在编辑器里点「下载 MP4」保存的视频，在浏览器的下载文件夹

升级：解压新版本后，把旧文件夹里的 projects 文件夹和 config.json 拷到新文件夹即可。
不要把自己的 config.json 发给别人（里面有 API Key）。

使用规则（请务必遵守）：
1. 不要录制含有客户或员工个人信息的界面。生成解说时，被点击元素上的文字、输入框里填的内容、
   页面标题和网址会发送给你在设置里选的 AI 服务商；画面上的打码不影响这些文字。
   （密码类输入框例外，录制时就已替换成 ••••，不会保存也不会发送。）
2. 内部资料和保密信息不要交给境外 AI 服务商处理。可以自己写解说词，或者在设置里改用
   本机运行的 Ollama，这样解说全程不出本机。
3. 录音功能会打开麦克风。如果可能录到其他人说话，请事先告知对方。

免责声明：
本软件在你的电脑上运行，但部分功能会调用第三方公司的在线服务（AI 生成解说、翻译和改写，
AI 配音，下载语音识别模型），相关的文字内容会发送到这些公司的服务器。这些服务的可用性、
费用、数据处理方式和使用条款由提供服务的公司决定，不受本软件控制。
使用前请自行确认：要处理的内容可以发送给外部服务，并且符合所在地的法律法规（包括个人信息
与数据保护的规定）、公司规定和保密义务。本软件按现状提供，不作任何保证；因使用本软件产生
的法律风险、违规责任和其他后果，由使用者个人承担。
（编辑器里 ⚙ 设置 → 关于 也能看到这段说明。）

第三方组件与许可证：
本软件包含以下开源组件，版权归各自作者所有：
- PyMuPDF（AGPL-3.0）：读取 PDF
- ffmpeg（GPLv3）：音视频编解码
- edge-tts（LGPL-3.0）、python-bidi（LGPL-3.0）：在线配音、从右向左文字排版
- pyttsx3（MPL-2.0）：离线配音兜底
- faster-whisper、CTranslate2、Pillow、FastAPI、pydantic、python-pptx、fontTools、
  onnxruntime、arabic-reshaper（MIT）
- uvicorn、numpy、PyAV（BSD）；requests、huggingface-hub、aiohttp、python-multipart
  （Apache-2.0）；pywin32（PSF）
这些组件的源码和完整许可证文本，可向本软件的提供者索取。
StepCast 本身按 Apache License 2.0 发布（见 LICENSE.txt），源码：https://github.com/comlong/stepcast
"""


def version() -> str:
    import re
    m = re.search(r'FastAPI\(title="StepCast", version="([^"]+)"\)', (ROOT / "backend" / "main.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "dev"


def make_icon() -> None:
    ico = PACKAGING / "icon.ico"
    if ico.exists():
        return
    from PIL import Image
    img = Image.open(ROOT / "extension" / "icons" / "icon128.png").convert("RGBA")
    img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
    print("图标已生成", ico)


def make_zip(zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(APP_DIR.rglob("*")):
            if f.is_file():
                z.write(f, Path("StepCast") / f.relative_to(APP_DIR))
    print(f"压缩包：{zip_path}（{zip_path.stat().st_size / 1048576:.0f} MB）")


def add_model(name: str) -> None:
    """Put the speech recognition model into dist/StepCast/models/; download it first if it isn't on this computer (falls back to the China mirror if the official source fails)."""
    sys.path.insert(0, str(ROOT))
    from backend.services import asr
    src = asr.local_model(name)
    if src is None:
        print(f"本机还没有 {name} 模型，先下载…")
        src = asr._download(name, lambda f, m: print("  " + m))
    dst = APP_DIR / "models" / f"faster-whisper-{name}"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copyfile(f.resolve(), dst / f.name)       # the cache may contain symlinks; copy the real files
    mb = sum(f.stat().st_size for f in dst.iterdir()) / 1048576
    print(f"语音识别模型 {name}：{src} -> {dst}（{mb:.0f} MB）")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 StepCast")
    ap.add_argument("--with-model", nargs="?", const="small", default="", metavar="MODEL",
                    help="另外再打一个带语音识别模型的压缩包（默认 small）")
    args = ap.parse_args()
    make_icon()
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    subprocess.run([sys.executable, "-m", "PyInstaller", str(PACKAGING / "StepCast.spec"),
                    "--noconfirm", "--clean", "--distpath", str(DIST),
                    "--workpath", str(ROOT / "build" / "pyinstaller")], check=True, cwd=ROOT)

    shutil.copytree(ROOT / "extension", APP_DIR / "extension",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    (APP_DIR / "README.txt").write_text(README, encoding="utf-8-sig")
    shutil.copyfile(ROOT / "LICENSE", APP_DIR / "LICENSE.txt")

    # your own keys and projects must never end up in the package
    for leak in ("config.json", "projects"):
        assert not (APP_DIR / leak).exists(), f"{leak} 不应该出现在发布包里"
        assert not (APP_DIR / "_internal" / leak).exists(), f"{leak} 不应该出现在发布包里"

    size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
    print(f"\n完成：{APP_DIR}（{size / 1048576:.0f} MB）")
    make_zip(DIST / f"StepCast-{version()}-win64.zip")
    if args.with_model:
        add_model(args.with_model)
        make_zip(DIST / f"StepCast-{version()}-win64-with-model-{args.with_model}.zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
