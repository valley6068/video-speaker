"""Shared by the server (server.py) and the command-line client (vs.py).

Only the standard library is used here, so vs.py can import it from any Python.

Where things live:
  APP_DIR    this folder. Code only; nothing is ever written here.
  DATA_DIR   small TEXT files that may stay: settings, text cache, history, log.
             %LOCALAPPDATA%\\VideoSpeaker (Windows) or ~/.cache/videospeaker,
             or VIDEOSPEAKER_HOME.
  MEDIA_DIR  the ONE folder for pictures, video and audio. Everything in it is
             deleted soon after use, and the whole folder at every server start.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"


# ---------------------------------------------------------------- silence ---
# A process with no console (pythonw, or a detached server) gives every console
# child it starts a NEW console window: each ffmpeg call used to flash one on
# screen. CREATE_NO_WINDOW stops that. It is added to every Popen in this
# process, so yt-dlp's and pip's own subprocesses are covered too, and a new
# call written later cannot forget it.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_silent():
    if not IS_WIN or getattr(subprocess.Popen, "_vs_silent", False):
        return
    original = subprocess.Popen.__init__

    def init(self, *args, **kw):
        kw["creationflags"] = (kw.get("creationflags") or 0) | NO_WINDOW
        si = kw.get("startupinfo") or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                       # SW_HIDE, for GUI children too
        kw["startupinfo"] = si
        original(self, *args, **kw)

    subprocess.Popen.__init__ = init
    subprocess.Popen._vs_silent = True


def run(cmd, timeout=120, **kw):
    """subprocess.run that never shows a window and never hangs forever."""
    make_silent()
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, timeout=timeout, **kw)


# ----------------------------------------------------------------- config ---

def _default_data_dir():
    if os.environ.get("VIDEOSPEAKER_HOME"):
        return os.environ["VIDEOSPEAKER_HOME"]
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VideoSpeaker")
    return os.path.join(os.path.expanduser("~"), ".cache", "videospeaker")


DATA_DIR = _default_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DEFAULTS_FILE = os.path.join(APP_DIR, "config.default.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_config():
    """Defaults shipped with the app, then the user's own file, then env vars."""
    cfg = _read_json(DEFAULTS_FILE)
    cfg.update(_read_json(CONFIG_FILE))
    env = {"VIDEOSPEAKER_PORT": ("port", int),
           "VIDEOSPEAKER_PYTHON": ("python", str),
           "VIDEOSPEAKER_FFMPEG": ("ffmpeg", str),
           "VIDEOSPEAKER_WHISPER_DIR": ("whisper_dir", str),
           "VIDEOSPEAKER_PYTHONPATH": ("pythonpath", str),
           "VIDEOSPEAKER_MEDIA_MAX_AGE": ("media_max_age_sec", int)}
    for var, (key, kind) in env.items():
        if os.environ.get(var):
            try:
                cfg[key] = kind(os.environ[var])
            except ValueError:
                pass
    return cfg


def save_config(changes):
    """Write only the user's own settings file (never the shipped defaults)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = _read_json(CONFIG_FILE)
    for k, v in changes.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG_FILE)


CFG = load_config()
PORT = int(CFG.get("port") or 8765)
# 127.0.0.1, never "localhost": on Windows localhost tries IPv6 first and
# stalls about two seconds on every request.
BASE = "http://127.0.0.1:%d" % PORT
MEDIA_DIR = os.path.join(tempfile.gettempdir(), "VideoSpeaker", str(PORT))
MEDIA_MAX_AGE = int(CFG.get("media_max_age_sec") or 900)


def media_dir(*parts):
    p = os.path.join(MEDIA_DIR, *parts)
    os.makedirs(p, exist_ok=True)
    return p


def ffmpeg_exe():
    """Configured path, then PATH, then the copy bundled by imageio-ffmpeg."""
    p = CFG.get("ffmpeg")
    if p and os.path.exists(p):
        return p
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def server_python():
    """The Python that has the server's packages: the one the installer chose,
    else this one. pythonw.exe (no console at all) when it exists."""
    py = CFG.get("python") or sys.executable
    if IS_WIN:
        w = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(w):
            return w
    return py


# ----------------------------------------------------------------- tokens ---

def image_tokens(w, h):
    """Claude's price for one picture: about width*height/750 tokens, after it
    shrinks anything over 1568 px on the long side or ~1.15 megapixels."""
    w, h = float(w), float(h)
    scale = min(1.0, 1568.0 / max(w, h), (1150000.0 / (w * h)) ** 0.5)
    return int((w * scale) * (h * scale) / 750) + 1


def text_tokens(s):
    return (len(s or "") + 3) // 4
