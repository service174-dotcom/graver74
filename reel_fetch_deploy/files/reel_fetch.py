"""reel_fetch — скачивание Reels/Shorts/TikTok на диск роутера и транскрибация.

Модуль-аддон social_connector. Позволяет получить сам видеоряд и речь ролика,
минуя браузерный blob: и минуя авторизацию Instagram (yt-dlp работает анонимно).

Точки входа (совместимость с разными диспетчерами аддонов):
    GET  /addons/reel_fetch/fetch?url=...
    POST /addons {"module":"reel_fetch","action":"fetch","params":{...}}
    MCP  addons_call(module="reel_fetch", action="fetch", params={...})
    REST /reel?action=fetch&url=...   (через reel_sidecar.py)

Действия:
    fetch       — скачать ролик (+аудио, +опционально транскрипт)
    probe       — метаданные без скачивания
    transcribe  — транскрипт по уже скачанному файлу
    list        — что лежит в media
    clean       — удалить файлы старше N дней

Возвращает JSON: локальные пути + публичный URL через Caddy /media/*.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

MODULE = "reel_fetch"
DESCRIPTION = "Скачивание роликов (Reels/Shorts/TikTok) на диск и транскрибация речи"

MEDIA_DIR = Path(os.environ.get("REEL_MEDIA_DIR", "/opt/social_connector/media"))
PUBLIC_BASE = os.environ.get("REEL_PUBLIC_BASE", "https://social.78.29.36.63.sslip.io").rstrip("/")
PUBLIC_PATH = "/media"
LOCK_FILE = MEDIA_DIR / ".reel_fetch.lock"

ALLOWED_HOSTS = (
    "instagram.com", "instagr.am", "cdninstagram.com",
    "tiktok.com", "vt.tiktok.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "vk.com", "vkvideo.ru", "rutube.ru",
    "twitter.com", "x.com", "facebook.com", "fb.watch",
    "threads.net", "likee.video", "dzen.ru",
)
ALLOW_ANY = os.environ.get("REEL_ALLOW_ANY", "0") == "1"

MAX_FILESIZE = int(os.environ.get("REEL_MAX_FILESIZE", str(300 * 1024 * 1024)))


class ReelError(Exception):
    """Ожидаемая ошибка с человекочитаемым текстом."""


# --------------------------------------------------------------------------- util


def _ensure_media_dir() -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(MEDIA_DIR, 0o750)
    return MEDIA_DIR


@contextlib.contextmanager
def _download_lock(wait_seconds: int = 120):
    """Ролики качаем по одному: 4 vCPU и общий канал на роутере."""
    _ensure_media_dir()
    fh = open(LOCK_FILE, "w")
    deadline = time.time() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise ReelError("Занято: другой ролик уже скачивается, повторите через минуту")
                time.sleep(1.0)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _check_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ReelError("Не передан url")
    if len(url) > 2048:
        raise ReelError("Слишком длинный url")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ReelError("Разрешены только http/https ссылки")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ReelError("Не разобрать хост в url")
    if not ALLOW_ANY and not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise ReelError(f"Хост {host} не в списке разрешённых (REEL_ALLOW_ANY=1 чтобы снять ограничение)")
    return url


def _safe_stem(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return value.strip("._-")[:80] or "media"


def _public_url(path: Path) -> str:
    return f"{PUBLIC_BASE}{PUBLIC_PATH}/{path.name}"


def _rel(path: Path) -> str:
    try:
        return str(path)
    except Exception:  # pragma: no cover
        return ""


def _which(name: str) -> str | None:
    return shutil.which(name)


# --------------------------------------------------------------------------- yt-dlp


def _ydl_opts(audio_only: bool, max_seconds: int | None) -> dict:
    _ensure_media_dir()
    opts = {
        "outtmpl": str(MEDIA_DIR / "%(extractor)s_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "restrictfilenames": False,
        "windowsfilenames": False,
        "max_filesize": MAX_FILESIZE,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        "geo_bypass": True,
    }
    if audio_only:
        opts["format"] = "ba[ext=m4a]/ba/b"
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
    if max_seconds:
        opts["match_filter"] = lambda info, *, incomplete=False: (
            None if not info.get("duration") or info["duration"] <= max_seconds
            else f"слишком длинное видео ({info['duration']} с > {max_seconds})"
        )
    return opts


def _pick_output(info: dict) -> Path:
    """yt-dlp кладёт рядом файлы разных расширений — берём реально существующий."""
    stem = Path(str(MEDIA_DIR / "%(extractor)s_%(id)s" % {})) if False else None  # noqa: F841
    base = MEDIA_DIR / f"{_safe_stem(info.get('extractor', 'media'))}_{_safe_stem(info.get('id', ''))}"
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        candidate = Path(str(base) + ext)
        if candidate.exists():
            return candidate
    # запасной вариант: самый свежий файл с id в имени
    token = _safe_stem(info.get("id", ""))
    if token:
        found = sorted(MEDIA_DIR.glob(f"*{token}*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return found[0]
    raise ReelError("Файл скачался, но не найден на диске (проверьте права на media)")


def _download(url: str, audio_only: bool = False, max_seconds: int | None = None) -> tuple[dict, Path]:
    try:
        import yt_dlp  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise ReelError("yt-dlp не установлен в venv коннектора") from exc

    with _download_lock():
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(audio_only, max_seconds)) as ydl:
                info = ydl.extract_info(url, download=True)
        except ReelError:
            raise
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                raise ReelError("Instagram/сервис ограничил частоту (429). Подождите 10–15 минут") from exc
            if "login" in msg.lower() or "requested content is not available" in msg.lower():
                raise ReelError("Сервис требует вход либо контент приватный") from exc
            raise ReelError(f"Не удалось скачать: {msg[:300]}") from exc

    if not info:
        raise ReelError("Пустой ответ от сервиса")
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    return info, _pick_output(info)


# --------------------------------------------------------------------------- ffmpeg / ASR


def _ffmpeg(*args: str, timeout: int = 120) -> None:
    exe = _which("ffmpeg")
    if not exe:
        raise ReelError("ffmpeg не найден в системе")
    proc = subprocess.run([exe, "-y", "-v", "error", *args], capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise ReelError(f"ffmpeg: {proc.stderr.decode('utf-8', 'replace')[:300]}")


def _extract_audio(video: Path) -> Path:
    out = video.with_suffix(".m4a")
    if out.exists() and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    _ffmpeg("-i", str(video), "-vn", "-c:a", "aac", "-b:a", "96k", "-ac", "1", str(out))
    return out


def _to_wav(audio: Path) -> Path:
    out = audio.with_suffix(".16k.wav")
    if out.exists():
        return out
    _ffmpeg("-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out))
    return out


def _transcribe_faster_whisper(wav: Path, language: str | None) -> str:
    from faster_whisper import WhisperModel  # noqa: WPS433

    model_dir = os.environ.get("REEL_WHISPER_MODEL", "small")
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", download_root=os.environ.get(
        "REEL_WHISPER_CACHE", "/opt/social_connector/models"))
    segments, _info = model.transcribe(str(wav), language=language, vad_filter=True, beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


def _transcribe_vosk(wav: Path, language: str | None) -> str:
    import wave
    from vosk import KaldiRecognizer, Model  # noqa: WPS433

    model_path = os.environ.get("REEL_VOSK_MODEL", "/opt/social_connector/models/vosk-ru")
    model = Model(model_path)
    with wave.open(str(wav), "rb") as wf:
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(False)
        chunks = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                chunks.append(json.loads(rec.Result()).get("text", ""))
        chunks.append(json.loads(rec.FinalResult()).get("text", ""))
    return " ".join(c.strip() for c in chunks if c).strip()


def _transcribe_addon(audio: Path, language: str | None) -> str | None:
    """Переиспользуем уже развёрнутый в коннекторе media_transcribe, если он умеет."""
    try:
        from . import media_transcribe  # type: ignore  # noqa: WPS433
    except Exception:
        try:
            import media_transcribe  # type: ignore  # noqa: WPS433
        except Exception:
            return None
    for name in ("transcribe", "run_action", "run", "handle"):
        fn = getattr(media_transcribe, name, None)
        if not callable(fn):
            continue
        for kwargs in ({"path": str(audio), "language": language}, {"audio": str(audio)}, {}):
            try:
                result = fn("transcribe", **kwargs) if name in ("run_action", "run", "handle") else fn(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            text = result.get("text") if isinstance(result, dict) else result
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def transcribe_file(path: str | Path, language: str | None = None) -> dict:
    src = Path(path)
    if not src.is_absolute():
        src = MEDIA_DIR / src.name
    if not src.exists():
        raise ReelError(f"Файл не найден: {src.name}")
    audio = src if src.suffix.lower() in (".m4a", ".mp3", ".wav", ".opus") else _extract_audio(src)
    wav = _to_wav(audio)
    errors = []
    text = None
    for engine, fn in (("faster-whisper", _transcribe_faster_whisper), ("vosk", _transcribe_vosk)):
        try:
            text = fn(wav, language)
            if text:
                return {"ok": True, "engine": engine, "language": language, "text": text,
                        "chars": len(text), "file": _rel(src), "audio": _rel(audio)}
        except Exception as exc:
            errors.append(f"{engine}: {str(exc)[:160]}")
    fallback = _transcribe_addon(audio, language)
    if fallback:
        return {"ok": True, "engine": "media_transcribe", "language": language, "text": fallback,
                "chars": len(fallback), "file": _rel(src), "audio": _rel(audio)}
    return {"ok": False, "error": "Ни один ASR-движок не сработал", "details": errors,
            "audio": _rel(audio), "hint": "На роутере: pip install faster-whisper (флаг --with-whisper)"}


# --------------------------------------------------------------------------- actions


def action_fetch(url: str = "", audio_only: bool = False, transcribe: bool = False,
                 language: str | None = None, max_seconds: int | None = None,
                 keep_video: bool = True, **_: object) -> dict:
    url = _check_url(url)
    started = time.time()
    info, path = _download(url, audio_only=bool(audio_only), max_seconds=max_seconds)
    audio = path if path.suffix.lower() in (".m4a", ".mp3", ".opus") else _extract_audio(path)

    result: dict = {
        "ok": True,
        "id": info.get("id"),
        "extractor": info.get("extractor"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "webpage_url": info.get("webpage_url") or url,
        "file": _rel(path),
        "file_bytes": path.stat().st_size if path.exists() else None,
        "file_url": _public_url(path),
        "audio": _rel(audio),
        "audio_url": _public_url(audio),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if transcribe:
        asr = transcribe_file(audio, language)
        result["transcript"] = asr.get("text") if asr.get("ok") else None
        result["transcript_engine"] = asr.get("engine")
        if not asr.get("ok"):
            result["transcript_error"] = asr.get("error")
            result["transcript_details"] = asr.get("details")
    if not keep_video and audio_only:
        with contextlib.suppress(OSError):
            path.unlink()
    return result


def action_probe(url: str = "", **_: object) -> dict:
    url = _check_url(url)
    try:
        import yt_dlp  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise ReelError("yt-dlp не установлен") from exc
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                           "noplaylist": True, "socket_timeout": 25}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("entries"):
        info = info["entries"][0]
    return {"ok": True, "id": (info or {}).get("id"), "title": (info or {}).get("title"),
            "uploader": (info or {}).get("uploader"), "duration": (info or {}).get("duration"),
            "formats": len((info or {}).get("formats") or []), "webpage_url": url}


def action_transcribe(file: str = "", url: str = "", language: str | None = None, **_: object) -> dict:
    if url and not file:
        fetched = action_fetch(url=url, audio_only=True, language=language)
        file = fetched["audio"]
    return transcribe_file(file, language)


def action_list(limit: int = 50, **_: object) -> dict:
    _ensure_media_dir()
    files = sorted((p for p in MEDIA_DIR.iterdir() if p.is_file() and not p.name.startswith(".")),
                   key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, int(limit))]
    return {"ok": True, "dir": str(MEDIA_DIR), "count": len(files), "items": [
        {"name": p.name, "bytes": p.stat().st_size,
         "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime)),
         "url": _public_url(p)} for p in files]}


def action_clean(days: int = 7, **_: object) -> dict:
    _ensure_media_dir()
    cutoff = time.time() - max(0, int(days)) * 86400
    removed = []
    for p in list(MEDIA_DIR.iterdir()):
        if p.is_file() and not p.name.startswith(".") and p.stat().st_mtime < cutoff:
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(p.name)
    return {"ok": True, "removed": len(removed), "names": removed[:50]}


ACTIONS = {
    "fetch": action_fetch,
    "probe": action_probe,
    "transcribe": action_transcribe,
    "list": action_list,
    "clean": action_clean,
}

ACTIONS_SCHEMA = {
    "fetch": {"url": "str (required)", "audio_only": "bool", "transcribe": "bool",
              "language": "str (ru/en)", "max_seconds": "int", "keep_video": "bool"},
    "probe": {"url": "str (required)"},
    "transcribe": {"file": "str", "url": "str", "language": "str"},
    "list": {"limit": "int"},
    "clean": {"days": "int"},
}


def health() -> dict:
    media_ok = MEDIA_DIR.exists() and os.access(MEDIA_DIR, os.W_OK)
    try:
        import yt_dlp  # noqa: WPS433
        ydl_version = yt_dlp.version.__version__
    except Exception:
        ydl_version = None
    asr = []
    try:
        import faster_whisper  # noqa: F401,WPS433
        asr.append("faster-whisper")
    except Exception:
        pass
    try:
        import vosk  # noqa: F401,WPS433
        asr.append("vosk")
    except Exception:
        pass
    return {
        "ok": bool(media_ok and ydl_version and _which("ffmpeg")),
        "module": MODULE,
        "media_dir": str(MEDIA_DIR),
        "media_writable": media_ok,
        "yt_dlp": ydl_version,
        "ffmpeg": _which("ffmpeg"),
        "asr": asr,
        "public_base": PUBLIC_BASE,
        "allow_any_host": ALLOW_ANY,
        "actions": sorted(ACTIONS),
    }


def describe() -> dict:
    return {"module": MODULE, "description": DESCRIPTION, "actions": ACTIONS_SCHEMA,
            "entrypoints": ["/addons/reel_fetch/<action>", "/reel?action=<action>"],
            "env": {"media_dir": str(MEDIA_DIR), "public_base": PUBLIC_BASE}}


# --------------------------------------------------------------------------- dispatcher


def run_action(action: str, **params: object) -> dict:
    fn = ACTIONS.get((action or "").strip())
    if not fn:
        raise ReelError(f"Неизвестное действие '{action}'. Доступно: {', '.join(sorted(ACTIONS))}")
    return fn(**params)


def run(action: str, **params: object) -> dict:
    return run_action(action, **params)


def handle(action: str, params: dict | None = None, **extra: object) -> dict:
    merged = dict(params or {})
    merged.update(extra)
    return run_action(action, **merged)


def handle_http(action: str, params: dict | None = None, **extra: object) -> dict:
    return handle(action, params, **extra)


def actions() -> list[str]:
    return sorted(ACTIONS)


def dispatch(action: str, params: dict | None = None) -> dict:
    """Точка входа addons_bridge (addons_call в MCP): dispatch(action, params)."""
    try:
        return handle(action, params)
    except ReelError as exc:
        return {"ok": False, "module": MODULE, "action": action, "error": str(exc)}
