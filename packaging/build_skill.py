"""Build the assistant skill (WorkBuddy etc.) with StepCast's source inside.

    .venv\\Scripts\\python.exe -X utf8 packaging\\build_skill.py

Output:
    dist/skill/stepcast-video/                the skill folder
        SKILL.md, scripts/, references/       from agent/workbuddy/stepcast-video/
        app/                                  StepCast's source, requirements.txt (no speech recognition),
                                              ffmpeg-requirements.txt (imageio-ffmpeg pinned by hash) and
                                              ffmpeg-sources.json (where to download ffmpeg by hand)
        extension/                            the Chrome extension
    dist/stepcast-video-<version>.zip         the zip to upload as a skill (must stay under 100 MB)

On first use the skill's `setup` command installs everything into a work folder the user picks: a Python environment
built with the assistant's Python from a PyPI mirror reachable from mainland China, ffmpeg, the program, settings and projects.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
SKILL_SRC = ROOT / "agent" / "workbuddy" / "stepcast-video"
MAX_BYTES = 100 * 1024 * 1024          # WorkBuddy's upload limit for a skill

# speech recognition isn't part of the skill (large native libraries, a separate model); ffmpeg comes pinned by hash
DROP_REQUIREMENTS = ("faster-whisper", "imageio-ffmpeg")
FFMPEG_SHA256 = "02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a"     # imageio-ffmpeg 0.6.0, win_amd64 wheel
FFMPEG_REQUIREMENT = f"imageio-ffmpeg==0.6.0 --hash=sha256:{FFMPEG_SHA256}"
# when every download by setup failed, the user downloads ffmpeg in the browser: the same wheel (setup checks its SHA-256)
# or a plain build, by network (cn = mainland China)
_WHEEL = "packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"
FFMPEG_SOURCES = {
    "sha256": FFMPEG_SHA256,
    "cn": ["https://mirrors.aliyun.com/pypi/" + _WHEEL, "https://pypi.tuna.tsinghua.edu.cn/" + _WHEEL,
           "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz"],
    "global": ["https://files.pythonhosted.org/" + _WHEEL, "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"],
}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def version() -> str:
    m = re.search(r'FastAPI\(title="StepCast", version="([^"]+)"\)', (ROOT / "backend" / "main.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "dev"


def requirements() -> str:
    keep = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = re.split(r"[<>=!;\[ ]", line.strip(), maxsplit=1)[0].lower()
        if name and not name.startswith("#") and name not in DROP_REQUIREMENTS:
            keep.append(line)
    return "\n".join(keep) + "\n"


def build(out_dir: Path) -> Path:
    """Write the skill folder to out_dir/stepcast-video and return it."""
    skill = out_dir / "stepcast-video"
    if skill.exists():
        shutil.rmtree(skill)
    shutil.copytree(SKILL_SRC, skill, ignore=IGNORE)
    app = skill / "app"
    app.mkdir()
    shutil.copytree(ROOT / "backend", app / "backend", ignore=IGNORE)
    shutil.copytree(ROOT / "static", app / "static", ignore=IGNORE)
    shutil.copyfile(ROOT / "app.py", app / "app.py")
    shutil.copyfile(ROOT / "LICENSE", app / "LICENSE.txt")
    (app / "requirements.txt").write_text(requirements(), encoding="utf-8")
    (app / "ffmpeg-requirements.txt").write_text(FFMPEG_REQUIREMENT + "\n", encoding="utf-8")
    (app / "ffmpeg-sources.json").write_text(json.dumps(FFMPEG_SOURCES, indent=2) + "\n", encoding="utf-8")
    (app / "VERSION").write_text(version() + "\n", encoding="utf-8")
    shutil.copytree(ROOT / "extension", skill / "extension", ignore=IGNORE)

    # your own keys and projects must never end up in the skill
    for leak in ("config.json", "projects", "models", ".venv"):
        found = [p for p in skill.rglob(leak)]
        assert not found, f"{leak} 不应该出现在技能包里：{found}"
    return skill


def make_zip(skill: Path, zip_path: Path) -> int:
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(skill.rglob("*")):
            if f.is_file():
                z.write(f, Path(skill.name) / f.relative_to(skill))
    return zip_path.stat().st_size


def main() -> int:
    skill = build(DIST / "skill")
    folder = sum(f.stat().st_size for f in skill.rglob("*") if f.is_file())
    zip_path = DIST / f"stepcast-video-{version()}.zip"
    size = make_zip(skill, zip_path)
    print(f"技能文件夹：{skill}（{folder / 1048576:.1f} MB）")
    print(f"压缩包：{zip_path}（{size / 1048576:.1f} MB）")
    if max(folder, size) >= MAX_BYTES:
        print("超过 100 MB，WorkBuddy 不收")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
