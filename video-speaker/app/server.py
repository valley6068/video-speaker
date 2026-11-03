"""Video Speaker: local server.

Paste a video link (YouTube, Shorts, TikTok, Twitch, ...) or a file on this PC,
see its details, get what is said, and see what is on screen.
Run: python server.py   then open http://127.0.0.1:8765
"""
import base64
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import common
from common import CFG, DATA_DIR, MEDIA_DIR, MEDIA_MAX_AGE, PORT, run, media_dir

common.make_silent()
os.makedirs(DATA_DIR, exist_ok=True)

# Started with pythonw there is no console: print() would go nowhere and some
# libraries crash writing to a missing stream. Send both to a small log file.
LOG_FILE = os.path.join(DATA_DIR, "server.log")
if sys.stdout is None or sys.stderr is None:
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.remove(LOG_FILE)
        _log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _log
        sys.stderr = sys.stderr or _log
    except Exception:
        pass

for _p in (CFG.get("pythonpath") or "").split(os.pathsep):
    if _p and os.path.isdir(_p):
        sys.path.insert(0, _p)

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp

APP_DIR = common.APP_DIR
VERSION = "2.0.0"
app = Flask(__name__, static_folder=None)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


# ---------------------------------------------------------- media folder ---
# Pictures, video and audio only ever live in MEDIA_DIR. It is emptied at every
# start (a crash cannot leave anything behind) and swept every minute.

def wipe_media():
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)


def sweep_media(max_age=None):
    max_age = MEDIA_MAX_AGE if max_age is None else max_age
    now = time.time()
    for root, dirs, files in os.walk(MEDIA_DIR, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if now - os.path.getmtime(fp) > max_age:
                    os.remove(fp)
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))      # only succeeds when empty
            except OSError:
                pass
    with _clip_lock:
        for u in [u for u, p in _clips.items() if not os.path.exists(p)]:
            _clips.pop(u, None)


def _sweeper():
    while True:
        time.sleep(max(5, min(60, MEDIA_MAX_AGE // 3)))
        try:
            sweep_media()
        except Exception as e:
            log("sweep failed: %s" % e)


# ------------------------------------------------------------ text cache ---
# Only TEXT is cached (info, transcripts, OCR text, answers), so asking the same
# thing twice is instant and free. Pictures are never cached.
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MAX = int(float(CFG.get("text_cache_mb") or 50) * 1e6)


def _cache_path(kind, key):
    h = hashlib.sha1(("%s|%s" % (kind, key)).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, "%s_%s.json" % (kind, h[:24]))


def cache_get(kind, key, max_age=None):
    p = _cache_path(kind, key)
    try:
        if max_age and time.time() - os.path.getmtime(p) > max_age:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_put(kind, key, value):
    p = _cache_path(kind, key)
    try:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except Exception:
        return
    trim_cache()


def trim_cache():
    files = []
    for f in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            files.append((os.path.getmtime(f), os.path.getsize(f), f))
        except OSError:
            pass
    total = sum(s for _, s, _ in files)
    for _, s, f in sorted(files):                # oldest first
        if total <= CACHE_MAX:
            break
        try:
            os.remove(f)
            total -= s
        except OSError:
            pass


# ------------------------------------------------------------- downloads ---

def normalise_url(url):
    """TikTok slideshow posts are shared as .../photo/<id>; yt-dlp refuses that
    shape but accepts the same id as .../video/<id>."""
    u = (url or "").strip().strip('"')
    if "tiktok.com/" in u and "/photo/" in u:
        u = u.replace("/photo/", "/video/", 1)
    return u


def is_local(url):
    return bool(url) and os.path.exists(url)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "skip_download": True,
    "retries": 3,
    "extractor_retries": 2,
    "socket_timeout": 20,
    "cachedir": os.path.join(DATA_DIR, "ytdlp-cache"),
    "http_headers": {"User-Agent": BROWSER_UA},
}
if common.ffmpeg_exe():
    YDL_BASE["ffmpeg_location"] = common.ffmpeg_exe()
# YouTube now needs a JavaScript runtime for most formats; yt-dlp only looks
# for deno by itself. Offer whichever one this PC has.
for _rt in ("deno", "node", "bun"):
    if shutil.which(_rt):
        YDL_BASE["js_runtimes"] = {_rt: {}}
        break


def _is_youtube(url):
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or "", re.I))


def client_variants(url):
    """Ordered recipes to try when a download is refused: (name, patch,
    drop_headers). The browser User-Agent is dropped for YouTube app clients
    because a mismatched UA gets them a 403."""
    if _is_youtube(url):
        yt = lambda *c: {"extractor_args": {"youtube": {"player_client": list(c)}}}
        v = [
            ("youtube default", {}, False),
            ("youtube ios/android/tv", yt("ios", "android", "tv"), True),
            ("youtube web_safari/mweb", yt("web_safari", "mweb"), True),
            ("youtube android_vr/tv_embedded", yt("android_vr", "tv_embedded"), True),
        ]
    else:
        v = [("default", {}, False)]
    for b in [x.strip() for x in (CFG.get("cookies_browser") or "").split(",") if x.strip()]:
        v.append(("cookies from " + b, {"cookiesfrombrowser": (b,)}, False))
    return v


def apply_variant(opts, variant):
    _name, patch, drop = variant
    o = dict(opts)
    if drop:
        o.pop("http_headers", None)
    o.update(patch)
    return o


_update_lock = threading.Lock()
_last_update = [0.0]


def auto_update_ytdlp():
    """Sites change and yt-dlp ships fixes. Update it (at most every 12 h) and
    reload it in this process, so a broken site can repair itself. Silent: pip
    runs with no window. Only ever installs the one package "yt-dlp"."""
    global yt_dlp
    if not CFG.get("auto_update_ytdlp", True):
        return False
    with _update_lock:
        if time.time() - _last_update[0] < 43200:
            return False
        _last_update[0] = time.time()
        cmd = [sys.executable, "-m", "pip", "install", "-U", "-q",
               "--disable-pip-version-check", "yt-dlp"]
        here = os.path.dirname(os.path.dirname(yt_dlp.__file__))
        if any(here and os.path.normcase(here) == os.path.normcase(p)
               for p in (CFG.get("pythonpath") or "").split(os.pathsep) if p):
            cmd[4:4] = ["--target", here]        # it lives in a custom folder
        try:
            r = run(cmd, timeout=240, text=True)
            if r.returncode != 0:
                log("yt-dlp update failed: " + (r.stderr or "")[-200:])
                return False
            for m in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                del sys.modules[m]
            import yt_dlp as _new
            yt_dlp = _new
            log("yt-dlp updated to " + _new.version.__version__)
            return True
        except Exception as e:
            log("yt-dlp update error: %s" % e)
            return False


def explain_failure(err):
    """Turn yt-dlp's wording into one plain sentence, or None."""
    s = str(err).lower()
    if "private" in s:
        return "this video is private"
    if any(k in s for k in ("removed", "deleted", "no longer available",
                            "video unavailable", "does not exist", "404")):
        return "this video was deleted or is not available"
    if "sign in" in s or "login" in s or "age" in s and "confirm" in s:
        return "the site wants a signed-in account to show this video"
    if "geo" in s or "country" in s:
        return "this video is blocked in this country"
    return None


def robust_extract(url, opts=None, download=False):
    """extract_info that tries hard. Order: every client recipe, the generic
    extractor, a media URL scraped from the page, the URL as a direct file,
    then one yt-dlp self-update and the whole ladder again."""
    o = dict(YDL_BASE)
    o.update(opts or {})
    if download:
        o["skip_download"] = False
    errs = []
    for round_no in (1, 2):
        for variant in client_variants(url):
            try:
                with yt_dlp.YoutubeDL(apply_variant(o, variant)) as ydl:
                    info = ydl.extract_info(url, download=download)
                if info:
                    return info
            except Exception as e:
                errs.append("%s: %s" % (variant[0], str(e)[:120]))
                if explain_failure(e) in ("this video is private",
                                          "this video was deleted or is not available"):
                    raise RuntimeError(explain_failure(e) + " (" + str(e)[:150] + ")")
        o2 = dict(o, force_generic_extractor=True)
        try:
            with yt_dlp.YoutubeDL(o2) as ydl:
                info = ydl.extract_info(url, download=download)
            if info and (info.get("url") or info.get("formats") or info.get("entries")):
                return info
        except Exception as e:
            errs.append("generic: " + str(e)[:120])
        try:
            media = scrape_media_url(url)
        except Exception as e:
            media = None
            errs.append("page scrape: " + str(e)[:80])
        if media and media != url:
            try:
                with yt_dlp.YoutubeDL(o2) as ydl:
                    info = ydl.extract_info(media, download=download)
                if info:
                    info.setdefault("webpage_url", url)
                    return info
            except Exception as e:
                errs.append("scraped link: " + str(e)[:80])
        if not download:
            di = direct_media_info(media or url)
            if di:
                return di
        if round_no == 1 and not auto_update_ytdlp():
            break
        errs.append("(updated yt-dlp and tried again)")
    raise RuntimeError("no readable video at that link. Tried: " + " | ".join(errs)[-600:])


_VIDEO_EXT_RE = re.compile(r"\.(mp4|m4v|mov|webm|mkv|avi|flv|ts|m3u8|mpd|mp3|m4a|wav|ogg|opus|aac)(\?|#|$)", re.I)
_MEDIA_URL_RE = re.compile(
    r"""https?://[^\s"'<>()]+?\.(?:mp4|m4v|mov|webm|mkv|m3u8|mpd|mp3|m4a|wav|ogg|opus|aac)(?:\?[^\s"'<>()]*)?""",
    re.I)


def http_get(url, timeout=20, tries=3, limit=2_000_000, headers=None):
    """GET with short backoff on network errors (not on 4xx answers)."""
    h = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(limit)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(0.8 * (i + 1))
    raise last


def scrape_media_url(page_url):
    """The first playable media URL in a page's own HTML, if any."""
    body = http_get(page_url).decode("utf-8", "replace")
    patterns = [
        r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)',
        r'<video[^>]+src=["\']([^"\']+)',
        r'<source[^>]+src=["\']([^"\']+)',
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'"hlsUrl"\s*:\s*"([^"]+)"',
        r'"playbackUrl"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        for m in re.finditer(pat, body, re.I):
            cand = m.group(1).replace("\\/", "/")
            if _VIDEO_EXT_RE.search(cand) or any(k in cand for k in ("m3u8", "mpd", "mp4")):
                return urllib.parse.urljoin(page_url, cand)
    m = _MEDIA_URL_RE.search(body)
    return urllib.parse.urljoin(page_url, m.group(0).replace("\\/", "/")) if m else None


def direct_media_info(url):
    """Treat the URL as a media file when the server says it serves media."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
    except Exception:
        ct = ""
    if not (ct.startswith(("video/", "audio/")) or "mpegurl" in ct or "dash" in ct
            or _VIDEO_EXT_RE.search(url)):
        return None
    ext = (re.search(r"\.([a-z0-9]{2,4})(\?|$)", url, re.I) or [None, "mp4"])[1]
    fmt = {"url": url, "ext": ext, "format_id": "direct", "vcodec": "unknown"}
    return {"id": hashlib.md5(url.encode()).hexdigest()[:11],
            "title": url.split("/")[-1].split("?")[0] or "media",
            "uploader": "web", "url": url, "ext": ext, "duration": None,
            "extractor_key": "Direct", "webpage_url": url,
            "formats": [fmt], "requested_formats": [fmt]}


def page_meta(url):
    """When no downloader can read a link, the page still describes itself:
    oEmbed and Open Graph tags give title, author, description and thumbnail."""
    out, tried = {}, []
    q = urllib.parse.quote(url, safe="")
    endpoints = []
    if _is_youtube(url):
        endpoints.append("https://www.youtube.com/oembed?format=json&url=" + q)
    if "tiktok.com" in url:
        endpoints.append("https://www.tiktok.com/oembed?url=" + q)
    html = ""
    try:
        html = http_get(url, limit=1_500_000).decode("utf-8", "replace")
        tried.append("page")
        m = re.search(r'<link[^>]+type=["\']application/json\+oembed["\'][^>]+href=["\']([^"\']+)', html, re.I)
        if m:
            endpoints.append(m.group(1).replace("&amp;", "&"))
    except Exception as e:
        tried.append("page (%s)" % str(e)[:40])
    for ep in endpoints:
        try:
            j = json.loads(http_get(ep, timeout=15).decode("utf-8", "replace"))
            tried.append("oEmbed")
            out.setdefault("title", j.get("title"))
            out.setdefault("uploader", j.get("author_name"))
            out.setdefault("thumbnail", j.get("thumbnail_url"))
            break
        except Exception:
            tried.append("oEmbed (failed)")

    def og(prop):
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % prop, html, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % prop, html, re.I))
        return _unescape(m.group(1)) if m else None

    if html:
        out["title"] = out.get("title") or og("og:title") or og("twitter:title")
        out["description"] = og("og:description") or og("description") or ""
        out["thumbnail"] = out.get("thumbnail") or og("og:image")
        if not out["title"]:
            m = re.search(r"<title>([^<]+)</title>", html, re.I)
            out["title"] = _unescape(m.group(1).strip()) if m else None
    return {k: v for k, v in out.items() if v}, tried


def _unescape(s):
    import html as _h
    return _h.unescape(s or "")


# --------------------------------------------------------------- ffmpeg ---

def ffmpeg():
    exe = common.ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg is not installed. Run the installer again, or "
                           "install it (Windows: winget install Gyan.FFmpeg) and retry.")
    return exe


def probe(path):
    """(duration_seconds, has_audio, has_video) from ffmpeg's own banner, so no
    separate ffprobe is needed."""
    try:
        r = run([ffmpeg(), "-hide_banner", "-i", path], timeout=30, text=True,
                encoding="utf-8", errors="replace")
        err = r.stderr or ""
    except Exception:
        return None, True, True
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    return dur, "Audio:" in err, "Video:" in err


# ------------------------------------------------------------- whisper ---
_whisper = None
_whisper_lock = threading.Lock()


def whisper_dir():
    d = CFG.get("whisper_dir") or os.path.join(DATA_DIR, "whisper-models")
    os.makedirs(d, exist_ok=True)
    return d


def whisper():
    """Loaded the first time a transcript really needs it, never at startup."""
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(CFG.get("whisper_model") or "small", device="cpu",
                                    compute_type="int8", download_root=whisper_dir(),
                                    cpu_threads=max(1, min(8, os.cpu_count() or 4)))
        return _whisper


def _audio_filter(media, af):
    out = os.path.join(media_dir("audio"), uuid.uuid4().hex + ".wav")
    try:
        run([ffmpeg(), "-y", "-loglevel", "error", "-i", media, "-af", af,
             "-ac", "1", "-ar", "16000", out], timeout=600)
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            return out
    except Exception:
        pass
    return media


def speech_filter(media):
    """Keep the voice band, quiet music/bass/hiss. No model, instant."""
    return _audio_filter(media, "highpass=f=250,lowpass=f=3600,afftdn=nf=-25,"
                                "dynaudnorm,volume=2")


def enhance_audio(media):
    """Lift quiet speech without changing what is said."""
    return _audio_filter(media, "afftdn=nf=-25,speechnorm=e=12.5:r=0.0004:l=1,"
                                "acompressor=threshold=-24dB:ratio=4:attack=5:release=120,"
                                "dynaudnorm=f=200:g=11")


def format_rich(segs):
    """Timestamps, speaker-change guesses, pauses, scene breaks, kept sound
    tags, and (?) on low-confidence lines instead of dropping them."""
    lines, speaker, last_end = [], 1, None
    for s in segs:
        gap = None if last_end is None else s["t"] - last_end
        if gap is not None and gap >= 3.5:
            lines += ["", "------ new clip / scene ------", ""]
            speaker = 1
        elif gap is not None and gap >= 1.5:
            lines.append("[pause %.0fs]" % round(gap))
            speaker = 2 if speaker == 1 else 1
        elif gap is not None and gap >= 0.8:
            speaker = 2 if speaker == 1 else 1
        low = s.get("lp", 0) <= -1.0 or s.get("nsp", 0) >= 0.6
        lines.append("[%s] Speaker %d: %s%s" % (mmss(s["t"]), speaker, s["text"],
                                                "  (?)" if low else ""))
        last_end = s.get("end", s["t"])
    return "\n".join(lines)


def listen_transcribe(media, cache_key, voiceonly=False, spoken=None, want="en",
                      enhance=False, rich=False):
    """Whisper listens to the audio. Cached as text per (video, options)."""
    hit = cache_get("listen", cache_key)
    if hit is not None:
        return hit
    if media is None:
        return []
    _dur, has_audio, _v = probe(media)
    if not has_audio:
        raise ValueError("this video has no audio track, so there is nothing to "
                         "listen to (some TikToks don't share their audio)")
    if voiceonly:
        media = speech_filter(media)
    if enhance:
        media = enhance_audio(media)
    m = whisper()
    lang = spoken if (spoken and spoken != "auto") else None
    if rich:
        extra = dict(vad_filter=True,
                     vad_parameters=dict(min_silence_duration_ms=600, speech_pad_ms=400),
                     temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                     beam_size=5, best_of=5, patience=1.0,
                     no_speech_threshold=0.75, log_prob_threshold=-1.6,
                     compression_ratio_threshold=2.6,
                     condition_on_previous_text=True, word_timestamps=True)
    elif enhance:
        extra = dict(vad_filter=True,
                     vad_parameters=dict(min_silence_duration_ms=700, speech_pad_ms=400),
                     no_speech_threshold=0.7, log_prob_threshold=-1.4,
                     condition_on_previous_text=True, beam_size=5)
    else:
        extra = dict(vad_filter=True)

    def collect(task, opts):
        segs, info = m.transcribe(media, task=task, language=lang, **opts)
        rows = []
        for s in segs:                       # lazy: errors surface while iterating
            t = s.text.strip()
            if not t:
                continue
            row = {"t": round(s.start, 2), "end": round(s.end, 2), "text": t}
            if rich:
                row["lp"] = round(getattr(s, "avg_logprob", 0.0) or 0.0, 3)
                row["nsp"] = round(getattr(s, "no_speech_prob", 0.0) or 0.0, 3)
                words = getattr(s, "words", None)
                if words:
                    row["t"], row["end"] = round(words[0].start, 2), round(words[-1].end, 2)
            rows.append(row)
        return rows, (lang or info.language)

    def attempt(task):
        # faster-whisper occasionally trips on one chunk with the rich settings;
        # step down to plainer settings instead of losing the whole clip.
        err = None
        for opts in (extra, dict(vad_filter=True), dict()):
            try:
                return collect(task, opts)
            except Exception as e:
                err = e
                log("whisper retry: %s" % e)
        # every setting failed: Whisper itself is broken, so say why instead of "nothing was said"
        raise ValueError("Whisper could not run (%s)" % str(err)[:200])

    out, src = attempt("transcribe")
    if want == "en" and src != "en":
        try:
            out2, src = attempt("translate")
        except ValueError:
            out2 = []                        # keep the original-language transcript
        out = out2 or out
    if out:                                  # never cache an empty failure
        cache_put("listen", cache_key, out)
    return out


# ------------------------------------------------------------- history ---
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(items):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def remember(entry):
    items = [h for h in load_history() if h.get("url") != entry["url"]]
    save_history([entry] + items[:29])


# ------------------------------------------------------------- safety ---
LAST_REQ = {"ts": time.time()}
_ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}


@app.before_request
def _guard():
    """Only this PC's own pages may talk to the server: a website in the
    browser must not be able to read files or change settings through it."""
    LAST_REQ["ts"] = time.time()
    if request.host not in _ALLOWED_HOSTS:
        return jsonify({"error": "wrong host"}), 403
    origin = request.headers.get("Origin")
    if origin and origin.split("//", 1)[-1] not in _ALLOWED_HOSTS:
        return jsonify({"error": "not allowed from another site"}), 403
    # Blind requests (<img>, no-cors fetch) carry no Origin, but browsers label every
    # request with Sec-Fetch-Site and a page cannot fake it. The command line sends none.
    if request.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
        return jsonify({"error": "not allowed from another site"}), 403


@app.errorhandler(Exception)
def _any_error(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    if code >= 500:
        log("error on %s: %s: %s" % (request.path, e.__class__.__name__, e))
    return jsonify({"error": "%s" % (getattr(e, "description", None) or e)}), code


def idle_watchdog():
    while True:
        time.sleep(60)
        if time.time() - LAST_REQ["ts"] > 15 * 60 and not busy():
            log("No activity for 15 minutes: closing.")
            wipe_media()
            os._exit(0)


def busy():
    try:
        import ai
        return any(j.get("state") not in ("done", "error") for j in ai.JOBS.values())
    except Exception:
        return False


@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "version": VERSION, "port": PORT,
                    "ffmpeg": bool(common.ffmpeg_exe()), "ocr": ocr_engine(),
                    "ai": AI_ON, "media_dir": MEDIA_DIR, "data_dir": DATA_DIR})


@app.route("/api/history")
def api_history():
    return jsonify(load_history())


@app.route("/api/history/clear")
def api_history_clear():
    save_history([])
    return jsonify({"ok": True})


@app.route("/api/history/delete")
def api_history_delete():
    url = normalise_url(request.args.get("url", ""))
    save_history([h for h in load_history() if h.get("url") != url])
    return jsonify({"ok": True})


@app.route("/api/clean", methods=["POST"])
def api_clean():
    """Remove every cached text, every picture, and the history list."""
    freed = 0
    for d in (CACHE_DIR, MEDIA_DIR):
        for dp, _, fs in os.walk(d):
            for f in fs:
                try:
                    freed += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    with _clip_lock:
        _clips.clear()
    save_history([])
    return jsonify({"ok": True, "freed_mb": round(freed / 1e6, 1)})


@app.route("/api/done", methods=["POST"])
def api_done():
    """The pictures were read: delete them now instead of waiting."""
    shutil.rmtree(os.path.join(MEDIA_DIR, "out"), ignore_errors=True)
    return jsonify({"ok": True})


# ----------------------------------------------------------------- info ---

def mmss(t):
    t = int(t or 0)
    return ("%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600
            else "%d:%02d" % (t // 60, t % 60))


def video_info(url):
    """Everything the page says about itself. Cached as text for 6 hours.
    When no downloader can read it, page metadata still comes back, marked
    partial, with what was tried and what is missing."""
    if is_local(url) and os.path.isfile(url):
        dur, _a, _v = probe(url)
        return {"id": None, "title": os.path.basename(url), "uploader": "this PC",
                "duration": dur, "site": "Local file", "description": "",
                "sub_langs": [], "auto_langs": [], "thumbnail": ""}
    hit = cache_get("info", url, max_age=6 * 3600)
    if hit:
        return hit
    try:
        info = robust_extract(url)
    except Exception as e:
        meta, tried = page_meta(url)
        if not meta:
            raise
        return dict(meta, partial=True, site=urllib.parse.urlparse(url).netloc,
                    reason=explain_failure(e) or str(e)[:300],
                    tried=["yt-dlp (every client)", "yt-dlp update",
                           "generic extractor", "page scrape"] + tried,
                    missing=["length", "subtitles", "video frames"])
    subs = sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat")
    autos = sorted((info.get("automatic_captions") or {}).keys())
    has_video = any((f.get("vcodec") or "none") != "none" for f in (info.get("formats") or []))
    result = {
        "id": info.get("id"), "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"), "views": info.get("view_count"),
        "likes": info.get("like_count"), "comments": info.get("comment_count"),
        "thumbnail": info.get("thumbnail"), "site": info.get("extractor_key"),
        "upload_date": info.get("upload_date"),
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
        "description": (info.get("description") or "")[:5000],
        "sub_langs": subs, "auto_langs": autos,
        "photo_post": bool(info.get("formats")) and not has_video,
        "thumbnails": [t.get("url") for t in (info.get("thumbnails") or []) if t.get("url")][-8:],
    }
    cache_put("info", url, result)
    return result


@app.route("/api/info")
def api_info():
    url = normalise_url(request.args.get("url", ""))
    if not url:
        return jsonify({"error": "No link given."}), 400
    try:
        result = video_info(url)
    except Exception as e:
        return jsonify({"error": "Could not read that link: " +
                        (explain_failure(e) or str(e)[:300])}), 400
    # the web page expects the older list-of-dicts shape for subtitles
    page = dict(result, sub_langs=[{"code": c, "auto": False} for c in result.get("sub_langs", [])]
                + [{"code": c, "auto": True} for c in result.get("auto_langs", [])
                   if c not in result.get("sub_langs", [])])
    if request.args.get("plain") == "1":
        page = result
    remember({"url": url, "title": result.get("title"), "site": result.get("site"),
              "duration": result.get("duration"), "thumbnail": result.get("thumbnail")})
    return jsonify(page)


@app.route("/api/comments")
def api_comments():
    url = normalise_url(request.args.get("url", ""))
    if not url:
        return jsonify({"error": "No link given."}), 400
    opts = dict(YDL_BASE, getcomments=True,
                extractor_args={"youtube": {"max_comments": ["40", "all", "0", "0"]}})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": "Could not get comments: " + str(e)[:200]}), 400
    return jsonify({"comments": [{"author": c.get("author"), "text": c.get("text"),
                                  "likes": c.get("like_count") or 0}
                                 for c in (info.get("comments") or [])[:40]]})


@app.route("/api/storyboard")
def api_storyboard():
    """YouTube-style hover pictures along the timeline (web page only)."""
    url = normalise_url(request.args.get("url", ""))
    if not url or is_local(url):
        return jsonify({"frames": []})
    try:
        info = robust_extract(url)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400
    boards = [f for f in (info.get("formats") or []) if str(f.get("format_id", "")).startswith("sb")]
    if not boards:
        return jsonify({"frames": []})
    b = sorted(boards, key=lambda f: (f.get("height") or 0))[-1]
    t, frames = 0.0, []
    for frag in (b.get("fragments") or []):
        d = frag.get("duration") or 0
        frames.append({"url": frag.get("url"), "start": t, "duration": d})
        t += d
    return jsonify({"rows": b.get("rows") or 5, "cols": b.get("columns") or 5,
                    "w": b.get("width") or 160, "h": b.get("height") or 90, "frames": frames})


# ----------------------------------------------------------- transcript ---

def pick_lang(available, wanted):
    if not available:
        return None
    if wanted in available:
        return wanted
    for lang in available:
        if lang.startswith(wanted):
            return lang
    for pref in ("en", "en-orig"):
        for lang in available:
            if lang.startswith(pref):
                return lang
    return sorted(available)[0]


def fetch_captions(url, wanted):
    """The site's own words, free and instant. Uploaded subtitles first, then
    automatic captions. Returns (segments, lang, source, tried)."""
    hit = cache_get("captions", url + "|" + wanted, max_age=24 * 3600)
    if hit:
        return hit["segs"], hit["lang"], hit["source"], ["cache"]
    tried = []
    try:
        info = robust_extract(url)
    except Exception as e:
        return [], None, None, ["captions: " + (explain_failure(e) or str(e)[:100])]
    for kind, label in (("subtitles", "subtitles"), ("automatic_captions", "auto-captions")):
        tracks = {k: v for k, v in (info.get(kind) or {}).items() if k != "live_chat"}
        lang = pick_lang(set(tracks), wanted)
        if not lang:
            tried.append(label + ": none")
            continue
        segs = None
        for fmt in ("json3", "vtt"):
            t = next((x for x in tracks[lang] if x.get("ext") == fmt and x.get("url")), None)
            if not t:
                continue
            for wait in (0, 3, 8):           # YouTube rate-limits caption calls (429)
                time.sleep(wait)
                try:
                    raw = http_get(t["url"], timeout=30, tries=2).decode("utf-8", "replace")
                    segs = parse_json3(raw) if fmt == "json3" else parse_vtt(raw)
                    break
                except Exception as e:
                    if "429" not in str(e):
                        break
            if segs:
                break
        if segs:
            cache_put("captions", url + "|" + wanted,
                      {"segs": segs, "lang": lang, "source": label})
            return segs, lang, label, tried + [label + ": ok"]
        tried.append(label + ": could not download")
    return [], None, None, tried


def parse_json3(raw):
    data = json.loads(raw)
    out = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if text:
            out.append({"t": ev.get("tStartMs", 0) / 1000.0,
                        "end": (ev.get("tStartMs", 0) + ev.get("dDurationMs", 0)) / 1000.0,
                        "text": text})
    return out


def parse_vtt(content):
    out = []
    ts = re.compile(r"(?:(\d+):)?(\d+):(\d+)\.(\d+)\s*-->")
    for b in re.split(r"\n\n+", content):
        t, text_lines = None, []
        for l in [l for l in b.strip().splitlines() if l.strip()]:
            m = ts.search(l)
            if m:
                t = int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
                continue
            if t is not None and "-->" not in l:
                text_lines.append(re.sub(r"<[^>]+>", "", l).strip())
        text = " ".join(text_lines).strip()
        if t is not None and text and (not out or out[-1]["text"] != text):
            out.append({"t": float(t), "text": text})
    return out


_listen_locks = {}
_listen_guard = threading.Lock()


def web_listen(url, voiceonly=False, spoken="auto", want="en", enhance=False, rich=False):
    """Whisper on a web video: downloads only the small audio track. One job
    per video, so a second ask waits for the first instead of doubling work."""
    sig = "_vo%d_eh%d_ri%d_%s_%s" % (voiceonly, enhance, rich, spoken, want)
    key = hashlib.md5(("listen|" + url + sig).encode()).hexdigest()
    hit = cache_get("listen", key)
    if hit is not None:
        return hit
    with _listen_guard:
        lock = _listen_locks.setdefault(key, threading.Lock())
    with lock:
        hit = cache_get("listen", key)
        if hit is not None:
            return hit
        tmp = tempfile.mkdtemp(dir=media_dir("audio"))
        try:
            fmt = "bestaudio/download/best[vcodec^=avc1]/best[acodec!=none]/best"
            try:
                robust_extract(url, {"format": fmt, "outtmpl": os.path.join(tmp, "a.%(ext)s")},
                               download=True)
            except Exception as e:
                raise ValueError("the audio could not be downloaded (%s)"
                                 % (explain_failure(e) or str(e)[:200]))
            audio = next((os.path.join(tmp, f) for f in os.listdir(tmp)), None)
            if not audio:
                raise ValueError("the audio could not be downloaded")
            return listen_transcribe(audio, key, voiceonly=voiceonly, spoken=spoken,
                                     want=want, enhance=enhance, rich=rich)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def merge_caption_whisper(caps, wh):
    """Captions are the base; a Whisper line is added only where no caption
    covers that moment and no nearby caption already says the same words."""
    caps_sorted = sorted(caps, key=lambda s: s["t"])
    spans = []
    for i, c in enumerate(caps_sorted):
        cend = c.get("end")
        if cend is None or cend <= c["t"]:
            cend = caps_sorted[i + 1]["t"] if i + 1 < len(caps_sorted) else c["t"] + 6
        spans.append((c["t"] - 0.4, cend + 0.4))

    def words(s):
        return set(re.sub(r"[^\w\s]", " ", s.lower()).split())

    def said_already(text, t):
        ww = words(text)
        if not ww:
            return True
        for c in caps_sorted:
            cw = words(c["text"])
            if abs(c["t"] - t) <= 6 and cw and len(ww & cw) / min(len(ww), len(cw)) >= 0.5:
                return True
        return False

    out = list(caps)
    for w in wh:
        if not any(a <= w["t"] <= b for a, b in spans) and not said_already(w["text"], w["t"]):
            out.append({"t": w["t"], "text": w["text"], "end": w.get("end")})
    return sorted(out, key=lambda s: s["t"])


@app.route("/api/transcript")
def api_transcript():
    """What was said. Order: the site's subtitles, its auto-captions, then
    Whisper on this PC only when neither exists."""
    url = normalise_url(request.args.get("url", ""))
    start = float(request.args.get("start", 0) or 0)
    end = float(request.args.get("end", 0) or 0) or float("inf")
    wanted = request.args.get("lang", "en")
    mode = request.args.get("mode", "normal")             # normal | both | listen
    voiceonly = request.args.get("voiceonly") == "1"
    enhance = request.args.get("enhance") == "1"
    rich = request.args.get("rich") == "1"
    spoken = request.args.get("spoken", "auto")
    want = request.args.get("want", "en")
    if not url:
        return jsonify({"error": "No link given."}), 400
    opts = dict(voiceonly=voiceonly, spoken=spoken, want=want, enhance=enhance, rich=rich)

    def reply(lang, segs, source, tried):
        picked = [s for s in segs if start <= s["t"] <= end]
        r = {"lang": lang, "source": source, "tried": tried, "segments": picked,
             "total_segments": len(segs)}
        if rich:
            r["rich"] = format_rich(picked)
        return jsonify(r)

    try:
        if is_local(url):
            key = hashlib.md5(("%s|%s|%s" % (url, os.path.getmtime(url), sorted(opts.items()))).encode()).hexdigest()
            return reply("AI-listened", listen_transcribe(url, key, **opts), "whisper", ["whisper"])
        caps, lang, source, tried = ([], None, None, []) if mode == "listen" else fetch_captions(url, wanted)
        if mode == "both":
            try:
                wh = web_listen(url, **opts)
            except Exception as e:
                if not caps:
                    raise
                wh, tried = [], tried + ["whisper failed: %s" % str(e)[:80]]
            merged = merge_caption_whisper(caps, wh) if wh else caps
            return reply("captions+AI (%s)" % (lang or "no captions"), merged,
                         "both", tried + ["whisper"])
        if caps:
            return reply(lang, caps, source, tried)
        segs = web_listen(url, **opts)
        return reply("AI-listened", segs, "whisper", tried + ["whisper (on this PC)"])
    except Exception as e:
        return jsonify({"error": "No transcript: " + (explain_failure(e) or str(e)[:300]),
                        "tried": locals().get("tried") or []}), 400


# ---------------------------------------------------------------- frames ---
# Pictures cost tokens by their pixel count, so every picture is made as small
# as it can be while still showing what matters. Full-size copies exist only
# for local OCR, inside MEDIA_DIR, and are deleted soon after.

_clips = {}                  # url -> local file of the video (frames are cut from it)
_clip_lock = threading.Lock()
_streams = {}                # url -> ((stream_url, headers), time)
LOCAL_CLIP_MAX = 20 * 60     # shorter than this: download once, cut frames locally
VIDEO_FMT = "bv*[ext=mp4]/bv*/b"


def stream_url(url):
    """(direct media URL, headers) for seeking a long video without
    downloading it. Links expire, so cached ~30 minutes."""
    hit = _streams.get(url)
    if hit and time.time() - hit[1] < 1800:
        return hit[0]
    info = robust_extract(url, {"format": VIDEO_FMT, "format_sort": ["res:720"]})
    su, headers = info.get("url"), info.get("http_headers") or {}
    if not su and info.get("requested_formats"):
        f = info["requested_formats"][0]
        su, headers = f.get("url"), f.get("http_headers") or headers
    if not su:
        raise RuntimeError("no readable video stream")
    _streams[url] = ((su, headers), time.time())
    return su, headers


def local_clip(url):
    """The video downloaded once into MEDIA_DIR (video only, no audio needed).
    Some CDNs (TikTok) refuse direct reads by ffmpeg, so this always works."""
    if is_local(url):
        return url
    with _clip_lock:
        p = _clips.get(url)
        if p and os.path.exists(p):
            os.utime(p)                      # in use: keep it past the sweep
            return p
        tmp = tempfile.mkdtemp(dir=media_dir("clips"))
        try:
            robust_extract(url, {"format": VIDEO_FMT, "format_sort": ["res:1080", "ext:mp4"],
                                 "outtmpl": os.path.join(tmp, "v.%(ext)s")}, download=True)
        except Exception:
            # lower quality, or any file with a picture in it
            robust_extract(url, {"format": "worstvideo/worst",
                                 "outtmpl": os.path.join(tmp, "v.%(ext)s")}, download=True)
        files = [f for f in os.listdir(tmp) if not f.endswith(".part")]
        if not files:
            raise RuntimeError("could not download a playable copy")
        _clips[url] = os.path.join(tmp, files[0])
        return _clips[url]


def source_for(url, dur):
    """(input path or URL, ffmpeg header args) for frame grabs."""
    if is_local(url) or not dur or dur <= LOCAL_CLIP_MAX:
        return local_clip(url), []
    su, headers = stream_url(url)
    args = ["-user_agent", headers.get("User-Agent", BROWSER_UA)]
    extra = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items() if k.lower() != "user-agent")
    return su, args + (["-headers", extra] if extra else [])


def grab(src, hdr, t, out, width=None, crop=None):
    """One still at time t. crop = (x, y, w, h) as fractions of the frame."""
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append("crop=iw*%f:ih*%f:iw*%f:ih*%f" % (w, h, x, y))
    if width:
        vf.append("scale='min(%d,iw)':-2" % width)
    cmd = [ffmpeg(), "-y", "-loglevel", "error"] + hdr + ["-ss", "%.3f" % t, "-i", src,
                                                         "-frames:v", "1", "-q:v", "4"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    r = run(cmd + [out], timeout=90, text=True, errors="replace")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    raise RuntimeError(((r.stderr or "").strip().splitlines() or ["no picture came out"])[-1][:150])


def scene_times(src, threshold=0.3, limit=200):
    """Moments where the picture changes a lot, from one fast low-res pass."""
    r = run([ffmpeg(), "-hide_banner", "-i", src, "-an", "-vf",
             "scale=160:-2,select='gt(scene,%s)',showinfo" % threshold,
             "-vsync", "vfr", "-f", "null", "-"], timeout=600, text=True, errors="replace")
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", r.stderr or "")]
    return times[:limit]


def pick_times(dur, n, start=0.0, end=None, scenes=None):
    """n moments: the start, then scene changes, topped up with even spacing."""
    end = min(end or dur, dur) if dur else (end or 0)
    span = max(0.1, end - start)
    even = [start + span * (i + 0.5) / n for i in range(n)]
    if not scenes:
        return even
    cand = sorted({round(start + 0.2, 2)} | {round(s, 2) for s in scenes if start <= s < end})
    if len(cand) > n:                        # too many: keep an even spread of them
        cand = [cand[int(i * len(cand) / n)] for i in range(n)]
    for t in even:                           # too few: fill the biggest gaps
        if len(cand) >= n:
            break
        if all(abs(t - c) > span / (2 * n) for c in cand):
            cand.append(t)
    return sorted(cand)[:n]


def dhash(path, size=8):
    from PIL import Image
    with Image.open(path) as im:
        g = im.convert("L").resize((size + 1, size), Image.BILINEAR)
        px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return bits


def changed_box(a, b, pad=0.04):
    """The part of picture b that differs from a, as fractions, or None when
    nearly everything changed (then the whole frame is worth sending)."""
    from PIL import Image, ImageChops
    with Image.open(a) as ia, Image.open(b) as ib:
        sa = ia.convert("L").resize((96, 96))
        sb = ib.convert("L").resize((96, 96))
    diff = ImageChops.difference(sa, sb).point(lambda v: 255 if v > 28 else 0)
    box = diff.getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = [v / 96.0 for v in box]
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(1, x1 + pad), min(1, y1 + pad)
    if (x1 - x0) * (y1 - y0) > 0.45:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def out_dir():
    return tempfile.mkdtemp(dir=media_dir("out"))


def img_size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def describe(path, t, kind="frame", box=None):
    w, h = img_size(path)
    d = {"t": round(t, 2), "at": mmss(t), "path": path, "w": w, "h": h,
         "tokens": common.image_tokens(w, h), "kind": kind}
    if box:
        d["box"] = [round(v, 3) for v in box]
    return d


def raw_frames(url, times, dur, width=None):
    """Full-size (or width-capped) stills at the given times, in parallel.
    Returns [(t, path)] and a list of problems."""
    src, hdr = source_for(url, dur)
    raw = tempfile.mkdtemp(dir=media_dir("raw"))
    problems = []

    def one(i_t):
        i, t = i_t
        try:
            return t, grab(src, hdr, t, os.path.join(raw, "r%04d.jpg" % i), width=width)
        except Exception as e:
            problems.append("%s: %s" % (mmss(t), e))
            return t, None

    with ThreadPoolExecutor(max_workers=6 if hdr else 4) as pool:
        got = [g for g in pool.map(one, enumerate(times)) if g[1]]
    return got, problems


def photo_post_images(url, info):
    """A photo post has no video. Fetch the pictures it does publish."""
    out, d = [], out_dir()
    for i, u in enumerate((info.get("thumbnails") or [info.get("thumbnail")])[-1:]):
        try:
            data = http_get(u, timeout=30, limit=20_000_000)
            p = os.path.join(d, "slide%02d.jpg" % (i + 1))
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.save(p, quality=85)
            out.append(p)
        except Exception:
            pass
    return out


PHOTO_NOTE = ("This is a photo (slideshow) post. Only the first slide is published "
              "to anyone who is not logged in, so slides 2 and later cannot be "
              "fetched. To read them all: open the post in the TikTok app, save "
              "each slide (or screen-record while swiping), put the files in one "
              "folder, and run this command on that folder.")


@app.route("/api/frames")
def api_frames():
    """Small stills ready to be looked at.

    at=10,20       exact moments                 (otherwise spread across the video)
    n=6            how many when spreading       scene=1  prefer scene changes
    width=512      size of each picture          crop=x,y,w,h  fractions of the frame
    start/end      limit to a range              changes=1  send only what changed
    """
    url = normalise_url(request.args.get("url", ""))
    a = request.args
    width = max(64, min(1920, int(a.get("width", 512))))
    n = max(1, min(60, int(a.get("n", 6))))
    crop = tuple(float(v) for v in a["crop"].split(",")) if a.get("crop") else None
    try:
        info = video_info(url)
    except Exception as e:
        return jsonify({"error": "Could not open the video: " + (explain_failure(e) or str(e)[:300])}), 400
    if info.get("photo_post"):
        imgs = photo_post_images(url, info)
        return jsonify({"frames": [describe(p, 0, "slide") for p in imgs], "note": PHOTO_NOTE,
                        "partial": True, "dropped": 0})
    if info.get("partial"):
        return jsonify({"error": "No video frames: " + info.get("reason", "the link could not be read"),
                        "tried": info.get("tried")}), 400
    dur = float(info.get("duration") or 0)
    start = float(a.get("start") or 0)
    end = float(a.get("end") or 0) or dur
    if a.get("at"):
        times = [float(x) for x in a["at"].split(",") if x.strip()]
        times = [t for t in times if t >= 0 and (not dur or t <= dur)]
        scenes = False
    else:
        scenes = a.get("scene", "1") == "1"
        st = None
        if scenes and (is_local(url) or dur <= LOCAL_CLIP_MAX):
            try:
                st = scene_times(local_clip(url))
            except Exception as e:
                log("scene detection failed: %s" % e)
        times = pick_times(dur, n, start, end, st)
    if not times:
        return jsonify({"error": "No times inside the video."}), 400
    try:
        got, problems = raw_frames(url, times, dur, width=None if (crop or a.get("changes") == "1") else width)
    except Exception as e:
        return jsonify({"error": "Could not take pictures: " + (explain_failure(e) or str(e)[:300])}), 400

    from PIL import Image
    d, frames, dropped, prev_raw, prev_hash = out_dir(), [], 0, None, None
    for t, path in got:
        h = dhash(path)
        if prev_hash is not None and not a.get("at") and bin(h ^ prev_hash).count("1") <= 4:
            dropped += 1                     # same picture as the last one sent
            continue
        box = changed_box(prev_raw, path) if (a.get("changes") == "1" and prev_raw) else None
        use = crop or box
        o = os.path.join(d, "at_%07.1f.jpg" % t)
        with Image.open(path) as im:
            im = im.convert("RGB")
            if use:
                W, H = im.size
                x, y, w, hh = use
                im = im.crop((int(x * W), int(y * H), int((x + w) * W), int((y + hh) * H)))
            if im.width > width:
                im = im.resize((width, max(1, int(im.height * width / im.width))), Image.LANCZOS)
            im.save(o, quality=82)
        frames.append(describe(o, t, "changed-area" if box else ("crop" if crop else "frame"), box))
        prev_raw, prev_hash = path, h
    return jsonify({"frames": frames, "dropped": dropped, "problems": problems,
                    "partial": len(got) < len(times), "duration": dur})


@app.route("/api/sheet")
def api_sheet():
    """Many moments in ONE picture: a grid with a time on every cell."""
    from PIL import Image, ImageDraw, ImageFont
    url = normalise_url(request.args.get("url", ""))
    a = request.args
    cols, rows = [int(v) for v in (a.get("grid") or "3x3").lower().split("x")]
    cols, rows = max(1, min(6, cols)), max(1, min(6, rows))
    cell = max(120, min(640, int(a.get("cell", 320))))
    try:
        info = video_info(url)
    except Exception as e:
        return jsonify({"error": "Could not open the video: " + (explain_failure(e) or str(e)[:300])}), 400
    if info.get("photo_post"):
        imgs = photo_post_images(url, info)
        return jsonify({"frames": [describe(p, 0, "slide") for p in imgs], "note": PHOTO_NOTE,
                        "partial": True})
    if info.get("partial"):
        return jsonify({"error": "No video frames: " + info.get("reason", "the link could not be read")}), 400
    dur = float(info.get("duration") or 0)
    start, end = float(a.get("start") or 0), float(a.get("end") or 0) or dur
    n = cols * rows
    st = None
    if is_local(url) or dur <= LOCAL_CLIP_MAX:
        try:
            st = scene_times(local_clip(url))
        except Exception as e:
            log("scene detection failed: %s" % e)
    try:
        got, problems = raw_frames(url, pick_times(dur, n, start, end, st), dur, width=cell)
    except Exception as e:
        return jsonify({"error": "Could not take pictures: " + (explain_failure(e) or str(e)[:300])}), 400
    kept, prev = [], None
    for t, p in got:                         # drop repeats so every cell is new
        h = dhash(p)
        if prev is None or bin(h ^ prev).count("1") > 4:
            kept.append((t, p))
            prev = h
    if not kept:
        return jsonify({"error": "No pictures could be taken.", "problems": problems}), 400
    ims = [Image.open(p).convert("RGB") for _, p in kept]
    cw, ch = ims[0].size
    cols = min(cols, len(ims))
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cw, rows * ch), "black")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=max(12, ch // 12))
    except TypeError:
        font = ImageFont.load_default()
    for i, ((t, _), im) in enumerate(zip(kept, ims)):
        x, y = (i % cols) * cw, (i // cols) * ch
        sheet.paste(im.resize((cw, ch)), (x, y))
        label = mmss(t)
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + box[2] + 8, y + box[3] + 6), fill="black")
        draw.text((x + 4, y + 2), label, fill="yellow", font=font)
        im.close()
    o = os.path.join(out_dir(), "sheet.jpg")
    sheet.save(o, quality=80)
    d = describe(o, kept[0][0], "sheet")
    d["cells"] = [{"t": round(t, 2), "at": mmss(t)} for t, _ in kept]
    d["grid"] = "%dx%d" % (cols, rows)
    return jsonify({"sheet": d, "problems": problems, "duration": dur})


@app.route("/api/frame")
def api_frame():
    """One still at time t, for the web page."""
    url = normalise_url(request.args.get("url", ""))
    t = float(request.args.get("t", 0) or 0)
    if not url:
        return jsonify({"error": "No link given."}), 400
    try:
        dur = float(video_info(url).get("duration") or 0)
        src, hdr = source_for(url, dur)
        o = os.path.join(out_dir(), "frame.jpg")
        grab(src, hdr, t, o, width=1280)
    except Exception as e:
        return jsonify({"error": "Could not grab that frame: " + str(e)[:150]}), 500
    return send_file(o, mimetype="image/jpeg")


# ------------------------------------------------------------------- OCR ---
# Reading words off the screen on this PC: free, and text costs a fraction of
# what a picture of the same text costs. Windows' built-in OCR first, then
# Tesseract if installed. The vision model is only used when both fail.

OCR_PS1 = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $null = $t.Wait(-1); $t.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($engine -eq $null) { Write-Output '###NOENGINE'; exit 0 }
foreach ($p in (Get-Content -LiteralPath $args[0] -Encoding UTF8)) {
  if (-not $p) { continue }
  Write-Output ('###FILE ' + $p)
  try {
    $f = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
    $s = Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $d = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $b = Await ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $r = Await ($engine.RecognizeAsync($b)) ([Windows.Media.Ocr.OcrResult])
    foreach ($l in $r.Lines) { Write-Output $l.Text }
    $s.Dispose()
  } catch { Write-Output '###ERROR' }
}
"""
_ocr_kind = []


def ocr_engine():
    if not _ocr_kind:
        kind = None
        if common.IS_WIN and shutil.which("powershell"):
            kind = "windows"
        elif shutil.which("tesseract"):
            kind = "tesseract"
        _ocr_kind.append(kind)
    return _ocr_kind[0]


def ocr_images(paths):
    """{path: text} read on this PC. Empty text when nothing was readable."""
    out = {p: "" for p in paths}
    if not paths:
        return out
    kind = ocr_engine()
    if kind == "windows":
        work = tempfile.mkdtemp(dir=media_dir("ocr"))
        try:
            script = os.path.join(work, "ocr.ps1")
            lst = os.path.join(work, "files.txt")
            with open(script, "w", encoding="utf-8-sig") as f:
                f.write(OCR_PS1)
            with open(lst, "w", encoding="utf-8") as f:
                f.write("\n".join(os.path.abspath(p) for p in paths))
            r = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", script, lst], timeout=60 + 5 * len(paths),
                    text=True, encoding="utf-8", errors="replace")
            cur = None
            absmap = {os.path.abspath(p): p for p in paths}
            for line in (r.stdout or "").splitlines():
                if line.startswith("###NOENGINE"):
                    _ocr_kind[0] = "tesseract" if shutil.which("tesseract") else None
                    return ocr_images(paths) if _ocr_kind[0] else out
                if line.startswith("###FILE "):
                    cur = absmap.get(line[8:].strip())
                elif cur and not line.startswith("###"):
                    out[cur] += line.strip() + "\n"
        finally:
            shutil.rmtree(work, ignore_errors=True)
    elif kind == "tesseract":
        for p in paths:
            try:
                out[p] = run(["tesseract", p, "stdout"], timeout=60, text=True,
                             encoding="utf-8", errors="replace").stdout or ""
            except Exception:
                pass
    return {p: t.strip() for p, t in out.items()}


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")


def local_images(path):
    if os.path.isfile(path) and path.lower().endswith(IMAGE_EXTS):
        return [path]
    if os.path.isdir(path):
        return sorted(os.path.join(path, f) for f in os.listdir(path)
                      if f.lower().endswith(IMAGE_EXTS))[:200]
    return []


def screen_text(url, n=None, start=0.0, end=None):
    """Words on screen at many moments, read locally. Returns a dict with
    `items` [{t, at, text}] (repeats removed), and what could not be read."""
    key = "%s|%s|%s|%s" % (url, n, start, end)
    hit = cache_get("ocr", key, max_age=24 * 3600)
    if hit:
        return dict(hit, cached=True)
    if not ocr_engine():
        return {"items": [], "engine": None, "unreadable": [],
                "error": "No OCR on this PC (Windows OCR or Tesseract)."}
    note = None
    imgs = local_images(url) if is_local(url) else []
    if imgs:
        got = [(float(i), p) for i, p in enumerate(imgs)]
    else:
        info = video_info(url)
        if info.get("photo_post"):
            got = [(0.0, p) for p in photo_post_images(url, info)]
            note = PHOTO_NOTE
        else:
            dur = float(info.get("duration") or 0)
            end = min(end or dur, dur)
            span = max(1.0, end - start)
            n = n or int(min(60, max(8, span / 3)))       # text flashes past: look often
            st = None
            if is_local(url) or dur <= LOCAL_CLIP_MAX:
                try:
                    st = scene_times(local_clip(url), threshold=0.15)
                except Exception:
                    pass
            got, _p = raw_frames(url, pick_times(dur, n, start, end, st), dur)
    texts = ocr_images([p for _, p in got])
    items, unreadable, last_lines = [], [], set()
    for t, p in got:
        lines = [l for l in texts.get(p, "").splitlines() if l.strip()]
        new = [l for l in lines if l not in last_lines]
        if lines:
            last_lines = set(lines)
        if new:
            items.append({"t": round(t, 2), "at": mmss(t) if not imgs else os.path.basename(p),
                          "text": "\n".join(new)})
        elif not lines:
            unreadable.append(round(t, 2))
    result = {"items": items, "engine": ocr_engine(), "looked": len(got),
              "unreadable": unreadable, "note": note}
    cache_put("ocr", key, result)
    return result


@app.route("/api/text")
def api_text():
    url = normalise_url(request.args.get("url", ""))
    if not url:
        return jsonify({"error": "No link given."}), 400
    try:
        return jsonify(screen_text(url, int(request.args.get("n") or 0) or None,
                                   float(request.args.get("start") or 0),
                                   float(request.args.get("end") or 0) or None))
    except Exception as e:
        return jsonify({"error": "Could not read the screen: " + (explain_failure(e) or str(e)[:300])}), 400


# ---- AI assist (watch / prompt): optional, needs an Ollama key ------------
try:
    import ai as _ai
    AI_ON = _ai.register(app)
except Exception as _e:
    AI_ON = False
    log("AI assist is off (%s: %s)" % (_e.__class__.__name__, _e))


if __name__ == "__main__":
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:
        log("Port %d is already in use: another Video Speaker is running." % PORT)
        sys.exit(0)
    finally:
        s.close()
    wipe_media()
    threading.Thread(target=_sweeper, daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    log("Video Speaker %s at http://127.0.0.1:%d  media: %s" % (VERSION, PORT, MEDIA_DIR))
    app.run(host="127.0.0.1", port=PORT, threaded=True)
