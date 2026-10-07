"""Run all regression tests.

    .venv\\Scripts\\python.exe -X utf8 tests\\run_all.py            # the regular groups
    .venv\\Scripts\\python.exe -X utf8 tests\\run_all.py -k reveal  # only tests whose name contains "reveal"
    ... --exe    also test the packaged dist\\StepCast (build it first)
    ... --ppt    also test reveal export and slide animations with real PowerPoint (PowerPoint must not be open)
    ... --lint   run ruff first

All tests run in tests/_work/ with their own data folders and config.json, never touching the user's projects or settings;
LLMs and cloud voice services are faked, so no credits are spent.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = HERE / "_work" / "run"

TESTS = ["test_regress", "test_i18n", "test_i18n_core", "test_outro", "test_llm", "test_notes", "test_cancel",
         "test_cards", "test_review_fixes", "test_parallel_render", "test_video_steps", "test_video_steps2",
         "test_review2", "test_encoder_fallback", "test_stability", "test_reveal_unit", "test_switch_lang",
         "test_dialogue", "test_llm_cn", "test_tts_cloud", "test_review3", "test_second_subs", "test_notes_lang",
         "test_languages", "test_video_first", "test_upload_delete", "test_slide_timeline", "test_slide_anim_render", "test_playback",
         "test_tts_parallel"]
BAD = re.compile(r"\[FAIL\]|Traceback \(most recent call last\)|失败 \d+ 项|结果: 失败|有 \d+ 处不一致")


def prepare() -> None:
    """Copy the fixtures into the work folder (tests write next to them, so fixtures/ isn't used directly); anything in private/ is copied too."""
    WORK.mkdir(parents=True, exist_ok=True)
    for src in (HERE / "fixtures", HERE / "private"):
        if src.is_dir():
            shutil.copytree(src, WORK, dirs_exist_ok=True)


def powerpoint_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/NH"], capture_output=True, text=True,
                         errors="replace").stdout
    return "POWERPNT.EXE" in out.upper()


def lint() -> bool:
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff.exe")
    if not Path(ruff).exists() and not shutil.which("ruff"):
        print("== lint: 没装 ruff，跳过（.venv\\Scripts\\pip install ruff）")
        return True
    ok = True
    for args in (["--select", "F,E4,E7,E9", "--line-length", "130", "backend", "tools"],
                 ["--select", "F", "--ignore", "F401,F541", "tests"]):       # the tests are script-style; only check for real errors
        r = subprocess.run([ruff, "check", "--isolated", *args], cwd=ROOT, capture_output=True, text=True, errors="replace")
        if r.returncode:
            ok = False
            print(r.stdout[-3000:])
    print(f"== lint: {'通过' if ok else '有问题'}")
    return ok


def run(name: str) -> bool:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", str(HERE / f"{name}.py"), str(WORK)], cwd=HERE, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1500)
        out, code = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = str(e.stdout or "") + "\n超时", -1
    (WORK / f"{name}.log").write_text(out, encoding="utf-8")
    bad = [ln for ln in out.splitlines() if BAD.search(ln)]
    ok = code == 0 and not bad
    last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    print(f"== {name}: {'通过' if ok else '失败'}  exit={code}  {time.time() - t0:.0f}s  {last[:90]}", flush=True)
    for ln in bad[:8]:
        print("   ", ln[:200])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="StepCast 回归测试")
    ap.add_argument("-k", default="", help="只跑名字里带这段文字的测试")
    ap.add_argument("--exe", action="store_true", help="再测打包出来的 dist\\StepCast")
    ap.add_argument("--ppt", action="store_true", help="再测真 PowerPoint（要求 PowerPoint 没开着）")
    ap.add_argument("--lint", action="store_true", help="先跑 ruff")
    a = ap.parse_args()

    prepare()
    names = [t for t in TESTS if a.k in t]
    if a.exe:
        names.append("test_exe")
    if a.ppt:
        if powerpoint_running():
            print("== test_reveal_ppt / test_anim_ppt: PowerPoint 正开着，跳过（测试不会关掉你的 PowerPoint）")
        else:
            names += ["test_reveal_ppt", "test_anim_ppt"]
    failed = [] if not a.lint or lint() else ["lint"]
    for n in names:
        if not run(n):
            failed.append(n)
    print("\n" + ("全部通过" if not failed else f"失败 {len(failed)} 组：{', '.join(failed)}（日志在 {WORK}）"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
