"""Local speech recognition: faster-whisper.

Audio never leaves this computer. The model is looked up in this order:
1. models/faster-whisper-<size>/ next to the program — placed there by hand (for networks that can't download it, e.g. mainland China or company firewalls)
2. already downloaded in the HuggingFace cache (~/.cache/huggingface)
3. otherwise download it: the official source first, then the China mirror hf-mirror.com if that can't be reached (a mirror set in the settings is tried first)
small is about 460 MB, downloaded once and usable offline afterwards.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import config, i18n

Progress = Optional[Callable[[float, str], None]]

_model = None
_model_key: tuple = ()
_lock = threading.Lock()

MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 465, "medium": 1500, "large-v3": 3100}


class ASRError(RuntimeError):
    pass


_WHISPER_ALIAS = {"nb": "no"}     # Norwegian (written Bokmål, nb-NO) is called "no" in Whisper


def whisper_lang(code: str) -> Optional[str]:
    """zh-CN -> zh; empty, auto, or a language the model doesn't know (e.g. Irish) returns None (the model detects it)."""
    code = (code or "").strip()
    if not code or code == "auto":
        return None
    base = code.split("-")[0].lower()
    base = _WHISPER_ALIAS.get(base, base)
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except Exception:
        return base
    return base if base in _LANGUAGE_CODES else None


def installed() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _hf_cache_dir() -> Path:
    try:
        from huggingface_hub import constants
        return Path(constants.HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


OFFICIAL = "https://huggingface.co"
CN_MIRROR = "https://hf-mirror.com"
# download only the files CTranslate2 inference needs (the repository has other formats we don't use)
_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"]


def _repo(name: str) -> str:
    return f"Systran/faster-whisper-{name}"


def offline_dirs(name: str) -> List[Path]:
    """Where models are placed by hand: the models folder next to the exe (or the source)."""
    roots = []
    for r in (config.INSTALL_DIR, config.BASE_DIR):
        if r not in roots:
            roots.append(r)
    return [r / "models" / f"faster-whisper-{name}" for r in roots]


def _complete(d: Path) -> bool:
    return (d / "model.bin").is_file() and (d / "config.json").is_file()


def _cached_snapshot(name: str) -> Optional[Path]:
    snaps = _hf_cache_dir() / f"models--Systran--faster-whisper-{name}" / "snapshots"
    if not snaps.is_dir():
        return None
    for d in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if _complete(d):
            return d
    return None


def local_model(name: str) -> Optional[Path]:
    """Model folder available on this computer (placed by hand first, then the download cache); None if there is none."""
    for d in offline_dirs(name):
        if _complete(d):
            return d
    return _cached_snapshot(name)


def _model_cached(name: str) -> bool:
    return local_model(name) is not None


def status() -> Dict[str, Any]:
    name = config.get("asr_model", "small")
    local = local_model(name)
    return {
        "installed": installed(),
        "model": name,
        "cached": local is not None,
        "loaded": _model is not None and _model_key[:1] == (name,),
        "size_mb": MODEL_SIZES_MB.get(name, 0),
        "models_dir": str(local if local else _hf_cache_dir()),
        "offline_dir": str(offline_dirs(name)[0]),
    }


def _endpoints() -> List[str]:
    """Order of download sources: mirror from the settings → official source → China mirror."""
    order: List[str] = []
    mirror = (config.get("hf_endpoint") or "").strip().rstrip("/")
    for ep in (mirror, OFFICIAL, CN_MIRROR):
        if ep and ep not in order:
            order.append(ep)
    return order


def _reachable(endpoint: str, name: str, timeout: float = 6.0) -> bool:
    """Quickly check whether this source can be reached, so we don't wait minutes for the official source from mainland China."""
    import requests
    try:
        r = requests.get(f"{endpoint}/api/models/{_repo(name)}", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _download(name: str, progress: Progress = None) -> Path:
    """Download the model and return its folder.

    Call snapshot_download directly with endpoint: huggingface_hub reads the download URL once at import time,
    so setting HF_ENDPOINT afterwards has no effect (which is why the "model mirror" setting used to do nothing).
    """
    from huggingface_hub import constants, snapshot_download
    total_mb = MODEL_SIZES_MB.get(name, 0)
    tried: List[str] = []
    for ep in _endpoints():
        host = ep.split("//")[-1]
        if not _reachable(ep, name):
            tried.append(i18n.t("{host}（连不上）", host=host))
            continue
        if progress:
            progress(0.02, i18n.t("首次使用，正在从 {host} 下载语音识别模型 {model}（约 {mb}MB，只需一次）…",
                                  host=host, model=name, mb=total_mb or "?"))
        # mirrors don't support HuggingFace's new Xet transfer protocol (its servers can't be reached from mainland China either), so use plain HTTP downloads
        xet_before = constants.HF_HUB_DISABLE_XET
        constants.HF_HUB_DISABLE_XET = xet_before or ep != OFFICIAL
        stop = threading.Event()
        watcher = threading.Thread(target=_watch_download, args=(name, host, total_mb, progress, stop),
                                   daemon=True)
        watcher.start()
        try:
            path = snapshot_download(_repo(name), endpoint=ep, cache_dir=str(_hf_cache_dir()),
                                     allow_patterns=_FILES, etag_timeout=20)
            if _complete(Path(path)):
                return Path(path)
            tried.append(i18n.t("{host}（文件不完整）", host=host))
        except Exception as e:
            tried.append(f"{host}（{str(e)[:120]}）")
        finally:
            stop.set()
            constants.HF_HUB_DISABLE_XET = xet_before
    raise ASRError(i18n.t(
        "语音识别模型 {model} 下载失败。试过：{tried}\n"
        "网络下载不了时，可以把模型文件夹放到 {folder}（里面要有 model.bin 等文件），不用联网就能用。",
        model=name, tried="；".join(tried), folder=offline_dirs(name)[0]))


def _watch_download(name: str, host: str, total_mb: int, progress: Progress,
                    stop: threading.Event) -> None:
    """While downloading, check the cache folder size every second and report progress to the UI (hundreds of MB take minutes)."""
    if not progress:
        return
    blobs = _hf_cache_dir() / f"models--Systran--faster-whisper-{name}" / "blobs"
    while not stop.wait(1.0):
        try:
            done = sum(f.stat().st_size for f in blobs.iterdir() if f.is_file()) / 1048576
        except Exception:
            continue
        frac = min(1.0, done / total_mb) if total_mb else 0.0
        try:
            progress(0.02 + 0.07 * frac, i18n.t("正在从 {host} 下载语音识别模型：{done} / {total} MB",
                                                host=host, done=int(done), total=total_mb or "?"))
        except BaseException:
            return        # the user clicked Stop: the progress callback raises JobCancelled; here we just stop reporting


def get_model(progress: Progress = None):
    """Load the model (downloading it if needed), reused within the process."""
    global _model, _model_key
    if not installed():
        raise ASRError(i18n.t("未安装语音识别组件，请执行：pip install faster-whisper"))
    name = config.get("asr_model", "small")
    device = config.get("asr_device", "auto")
    key = (name, device)
    with _lock:
        if _model is not None and _model_key == key:
            return _model
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        t = time.time()
        path = local_model(name)
        if path is None:
            path = _download(name, progress)
        if progress:
            progress(0.09, i18n.t("加载语音识别模型 {model}…", model=name))
        from faster_whisper import WhisperModel
        try:
            _model = WhisperModel(
                str(path),                    # pass a local folder: no network access while loading
                device=device if device != "auto" else "auto",
                compute_type="int8",
            )
        except Exception as e:
            _model = None
            raise ASRError(i18n.t("语音识别模型加载失败：{error}\n模型目录：{folder}", error=e, folder=path))
        _model_key = key
        if progress:
            progress(0.1, i18n.t("模型就绪（{sec} 秒）", sec=f"{time.time() - t:.0f}"))
        return _model


# ---- text cleanup -------------------------------------------------------------

_ZH_FILLERS = r"(?:嗯+|啊+|呃+|额+|唔+|哦+|诶+|那个那个|就是说|然后呢)"  # i18n: ignore
_EN_FILLERS = r"(?:um+|uh+|erm+|hmm+|you know|i mean)"


def clean_text(text: str, language: str = "zh") -> str:
    """Remove filler words and normalise punctuation. Only fillers standing alone at the start of a sentence or between commas are removed, never normal words."""
    t = (text or "").strip()
    if not t:
        return ""
    lang = whisper_lang(language) or ("zh" if re.search(r"[一-鿿]", t) else "en")  # i18n: ignore
    if config.get("asr_remove_fillers", True):
        if lang == "zh":
            t = re.sub(rf"(^|[，,。！？!?；;\s])\s*{_ZH_FILLERS}\s*[，,。]?\s*", r"\1", t)
        else:
            t = re.sub(rf"(^|[,.!?;]\s*)\b{_EN_FILLERS}\b[,.]?\s*", r"\1", t, flags=re.I)
        # stutter repeats such as "we we" or "the the". Single-character reduplication in Chinese (kankan, xiexie, manman) is normal and kept
        t = re.sub(r"(?<!一)((?!一)[一-鿿]{2,3})\1", r"\1", t)  # i18n: ignore
        t = re.sub(r"\b(\w+)\s+\1\b", r"\1", t, flags=re.I)
    if lang in ("zh", "ja"):
        t = (t.replace(",", "，").replace("?", "？").replace("!", "！")
              .replace(";", "；").replace(":", "："))
        t = re.sub(r"\s+", "", t)
        t = re.sub(r"^[，。；]+", "", t)
        if t and t[-1] not in "。！？…":
            t = t.rstrip("，；") + "。"
    else:
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r"^[,.;]\s*", "", t)
        if t and t[-1] not in ".!?":
            t += "."
    return t


# ---- recognition ----------------------------------------------------------------

def transcribe(path: str | Path, language: str = "", progress: Progress = None,
               words: bool = True) -> Dict[str, Any]:
    """Returns {duration, language, text, segments:[{start,end,text,words:[{w,start,end}]}]}."""
    path = str(path)
    if not os.path.exists(path):
        raise ASRError(i18n.t("音频文件不存在"))
    model = get_model(progress)
    if progress:
        progress(0.12, i18n.t("识别语音中…"))
    # whisper's zh doesn't distinguish Simplified and Traditional Chinese and often mixes in Traditional characters; a prompt sentence in the right script pulls it back
    lc = (language or "").lower()
    prompt = None
    if lc in ("zh", "zh-cn", "zh-sg", "zh-hans"):
        prompt = "以下是普通话的句子，使用简体中文。"  # i18n: ignore
    elif lc in ("zh-tw", "zh-hk", "zh-hant"):
        prompt = "以下是普通話的句子，使用繁體中文。"  # i18n: ignore
    try:
        seg_iter, info = model.transcribe(
            path, language=whisper_lang(language), word_timestamps=words,
            vad_filter=True, beam_size=5, initial_prompt=prompt,
            vad_parameters={"min_silence_duration_ms": 400},
        )
        segments: List[Dict[str, Any]] = []
        total = max(0.1, float(info.duration or 0))
        for s in seg_iter:
            segments.append({
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
                "words": [{"w": w.word, "start": float(w.start), "end": float(w.end)}
                          for w in (s.words or [])],
            })
            if progress:
                progress(0.12 + 0.83 * min(1.0, float(s.end) / total),
                         i18n.t("识别中 {done}/{total} 秒", done=f"{float(s.end):.0f}", total=f"{total:.0f}"))
    except ASRError:
        raise
    except Exception as e:
        raise ASRError(i18n.t("语音识别失败：{error}", error=e))

    lang = info.language or whisper_lang(language) or ""
    sep = "" if lang in ("zh", "ja") else " "
    text = clean_text(sep.join(s["text"] for s in segments), lang)
    if progress:
        progress(1.0, i18n.t("识别完成，{n} 句", n=len(segments)))
    return {"duration": float(info.duration or 0), "language": lang,
            "text": text, "segments": segments}


def words_to_boundaries(segments: List[Dict[str, Any]], offset: float = 0.0,
                        language: str = "zh") -> List[Dict[str, Any]]:
    """Convert whisper's word timestamps into boundaries for subtitle alignment (relative to offset)."""
    out: List[Dict[str, Any]] = []
    for s in segments:
        ws = s.get("words") or []
        if not ws:
            out.append({"t": max(0.0, s["start"] - offset),
                        "d": max(0.05, s["end"] - s["start"]), "text": s["text"]})
            continue
        for w in ws:
            txt = w["w"].strip() if language in ("zh", "ja") else w["w"]
            if not txt:
                continue
            out.append({"t": max(0.0, w["start"] - offset),
                        "d": max(0.02, w["end"] - w["start"]), "text": txt})
    return out
