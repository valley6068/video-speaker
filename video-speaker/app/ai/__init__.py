"""Video Speaker: AI assist (optional, needs an Ollama key).

Adds `watch` (a vision model describes what is on screen), `prompt` (rebuild
text shown on screen), and two transcript helpers for the web page (sharpen,
analyze). Delete this folder and the rest of Video Speaker works unchanged.

Why corrections are a LIST and not a rewrite: a model asked for a "better"
transcript writes fluent text that quietly invents things. So it is asked for
specific word-level fixes, each naming the exact phrase, the fix and why, and a
fix is thrown away unless its phrase appears exactly once in the transcript.

Only these calls leave the PC, and only to https://ollama.com. Frames are read
by local OCR first, so pictures are sent only when OCR cannot read them.
"""
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request, send_from_directory

import common

AI_DIR = os.path.dirname(os.path.abspath(__file__))
OLLAMA_HOST = "https://ollama.com"
TAGS_URL = OLLAMA_HOST + "/api/tags"
CHAT_URL = OLLAMA_HOST + "/api/chat"
TIMEOUT = 300            # cloud models can think a long time on a big transcript
CHUNK_CHARS = 5000       # one request per chunk keeps the model accurate

bp = Blueprint("ai", __name__)
_lock = threading.Lock()


def host():
    """The RUNNING server module. server.py is started directly, so it is
    "__main__"; importing "server" would load a second copy of it."""
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "video_info"):
        return main
    import server as _vs
    return _vs


# ----------------------------------------------------------------- config ---

def cfg():
    return common.load_config()


def api_key():
    """A key saved in the app wins over the environment variable: an explicit
    action must beat ambient configuration."""
    return cfg().get("ollama_key") or os.environ.get("OLLAMA_API_KEY") or ""


def key_source():
    if common._read_json(common.CONFIG_FILE).get("ollama_key"):
        return "saved"
    return "env" if os.environ.get("OLLAMA_API_KEY") else "none"


NO_KEY = ("watch and prompt need an Ollama key (OLLAMA_API_KEY, or paste one in the "
          "app's AI panel). Free alternatives that need no key: info, transcript, "
          "text, sheet, frames.")


# ------------------------------------------------------------------- http ---

def _contexts():
    """Some antivirus tools break Python's certificate chain: try the bundled
    roots first, then the system store."""
    out = []
    try:
        import certifi
        out.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    try:
        out.append(ssl.create_default_context())
    except Exception:
        pass
    return out or [None]


def http_json(url, payload=None, key=None, timeout=TIMEOUT, tries=3):
    """POST json (GET when payload is None). Returns (data, error). Retries
    with backoff on 5xx and network errors: Ollama sometimes answers 500 to a
    request that works a moment later."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "VideoSpeaker"}
    if key:
        headers["Authorization"] = "Bearer " + key
    last = None
    for attempt in range(tries):
        for ctx in _contexts():
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    return json.loads(r.read().decode("utf-8", "replace")), None
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code in (401, 403):
                    if "subscription" in body:
                        return None, "That model needs a paid Ollama plan. Pick a different one."
                    return None, "Ollama refused the API key. Check it at ollama.com/settings/keys."
                if e.code == 404:
                    return None, "Ollama does not know that model. Pick another one."
                if e.code == 400 and "image" in body:
                    return None, "IMAGE_UNSUPPORTED"
                if 500 <= e.code < 600 or e.code == 429:
                    last = "Ollama replied %s: %s" % (e.code, body)
                    break
                return None, "Ollama replied %s: %s" % (e.code, body)
            except Exception as e:
                last = e
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1))
    if isinstance(last, str):
        return None, last + "  (tried %d times)" % tries
    return None, "Could not reach ollama.com (%s)" % (last,)


def chat(model, system, user, key, want_json=False):
    payload = {"model": model, "stream": False, "options": {"temperature": 0.1},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if want_json:
        payload["format"] = "json"
    data, err = http_json(CHAT_URL, payload, key)
    if err:
        return None, err
    return (((data or {}).get("message") or {}).get("content") or "").strip(), None


def parse_json(out):
    try:
        return json.loads(out)
    except Exception:
        m = re.search(r"\{.*\}", out or "", re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}


# ------------------------------------------------------------------ routes --

@bp.route("/ai/<name>")
def ai_files(name):
    if name not in ("panel.js", "panel.css"):     # never serve anything else here
        return jsonify({"error": "not found"}), 404
    resp = send_from_directory(AI_DIR, name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/ai/config")
def api_config():
    c, key = cfg(), api_key()
    return jsonify({"enabled": True, "has_key": bool(key),
                    "key_hint": ("…" + key[-4:]) if len(key) > 8 else ("set" if key else ""),
                    "source": key_source(), "has_env": bool(os.environ.get("OLLAMA_API_KEY")),
                    "model": c.get("model", ""), "vision_model": c.get("vision_model", "")})


@bp.route("/api/ai/config", methods=["POST"])
def api_config_set():
    body = request.get_json(silent=True) or {}
    changes = {}
    if "key" in body:
        changes["ollama_key"] = (body.get("key") or "").strip() or None
    for k in ("model", "vision_model"):
        if k in body:
            changes[k] = (body.get(k) or "").strip()
    with _lock:
        common.save_config(changes)
    return jsonify({"ok": True})


@bp.route("/api/ai/models")
def api_models():
    data, err = http_json(TAGS_URL, None, None, timeout=30)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"models": sorted(m.get("name") or m.get("model")
                                     for m in (data or {}).get("models", [])
                                     if m.get("name") or m.get("model"))})


@bp.route("/api/ai/test", methods=["POST"])
def api_test():
    body = request.get_json(silent=True) or {}
    model = (body.get("model") or cfg().get("model") or "").strip()
    if not model:
        return jsonify({"error": "Pick a model first."}), 400
    if not api_key():
        return jsonify({"error": "Add your Ollama API key first."}), 400
    out, err = chat(model, "Reply with exactly: ok", "Say ok.", api_key())
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True, "reply": (out or "")[:80], "model": model})


# ------------------------------------------------ transcript helpers (web) --

def context_block(info):
    """Names and jargon are usually written in the title and description."""
    if not isinstance(info, dict):
        return ""
    bits = []
    for label, k in (("Title", "title"), ("Channel", "uploader"), ("Channel", "channel"), ("Site", "site")):
        v = info.get(k)
        if v and not any(b.startswith(label + ":") for b in bits):
            bits.append("%s: %s" % (label, v))
    desc = (info.get("description") or "").strip()
    if desc:
        bits.append("Description (may contain the correct spellings of names and terms):\n" + desc[:2500])
    return "\n".join(bits)


def chunks(text, size=CHUNK_CHARS):
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out or [text]


def keep_fixes(parsed, text):
    """Only fixes that can be applied exactly once survive."""
    out = []
    for f in (parsed or {}).get("fixes", []):
        a, b = (f.get("from") or "").strip(), (f.get("to") or "").strip()
        if a and b and a != b and text.count(a) == 1:
            out.append({"from": a, "to": b, "why": (f.get("why") or "").strip()[:200],
                        "confidence": f.get("confidence", "medium")})
    return out


SHARPEN_SYSTEM = """You are a transcript proofreader. A speech-to-text system \
produced the transcript below from audio, so its mistakes are MIS-HEARINGS: \
words that sound similar to what was really said.

Your ONLY job is to find those mis-hearings and correct them.

Rules you must follow exactly:
- Only change something when the correct version SOUNDS LIKE what is written.
- Prefer names, brands, places and technical terms that appear in the context.
- Never rewrite for style, never shorten, never summarise, never translate.
- Never invent content that is not there. If nothing is wrong, return nothing.
- Keep the speaker's own words and grammar, including informal speech.

Return ONLY a JSON object of this exact shape:
{"fixes":[{"from":"<exact text as written>","to":"<corrected text>",\
"why":"<short reason>","confidence":"high|medium|low"}]}

"from" MUST be copied character-for-character from the transcript, and must be \
long enough to appear only once. If you are unsure, use "low" or leave it out."""


@bp.route("/api/ai/sharpen", methods=["POST"])
def api_sharpen():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    model = (body.get("model") or cfg().get("model") or "").strip()
    if not text:
        return jsonify({"error": "There is no transcript to check yet."}), 400
    if not model:
        return jsonify({"error": "Pick a model first."}), 400
    key = api_key()
    if not key:
        return jsonify({"error": "Add your Ollama API key first."}), 400
    ctx = context_block(body.get("info") or {})
    fixes, problems = [], []
    for i, part in enumerate(chunks(text), 1):
        user = ("CONTEXT ABOUT THIS VIDEO\n" + ctx + "\n\n" if ctx else "") + \
               "TRANSCRIPT PART %d\n%s" % (i, part)
        out, err = chat(model, SHARPEN_SYSTEM, user, key, want_json=True)
        if err:
            problems.append(err)
            continue
        fixes += keep_fixes(parse_json(out), text)
    if not fixes and problems:
        return jsonify({"error": problems[0]}), 502
    return jsonify({"fixes": fixes, "checked": len(chunks(text)), "problems": problems})


ANALYZE_SYSTEM = """You analyse a video using its transcript. Be concrete and \
detailed. Ground every statement in what was actually said: quote short \
fragments as evidence. If something is unclear or the transcript is incomplete, \
say so plainly instead of guessing. Do not pad, do not moralise, do not add a \
conclusion that repeats the rest."""


@bp.route("/api/ai/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    model = (body.get("model") or cfg().get("model") or "").strip()
    if not text:
        return jsonify({"error": "There is no transcript to work with yet."}), 400
    if not model:
        return jsonify({"error": "Pick a model first."}), 400
    key = api_key()
    if not key:
        return jsonify({"error": "Add your Ollama API key first."}), 400
    ctx = context_block(body.get("info") or {})
    ask = (body.get("question") or "").strip() or (
        "Give a detailed breakdown of this video:\n"
        "1. What it is actually about, in a few sentences.\n"
        "2. Everything that happens or is claimed, in order, with timestamps if the transcript has them.\n"
        "3. Names, places, numbers and terms mentioned, listed exactly.\n"
        "4. Anything the transcript leaves unclear or seems to have mis-heard.")
    user = ("CONTEXT ABOUT THIS VIDEO\n" + ctx + "\n\n" if ctx else "") + \
           "TRANSCRIPT\n" + text[:60000] + "\n\nTASK\n" + ask
    out, err = chat(model, ANALYZE_SYSTEM, user, key)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"text": out})


# ============================================================================
#  WATCHING: frames go to a vision model, which says what is on screen.
#  Verified models that see images: minimax-m3 (fastest, ~2.5 s a batch),
#  gemma4:31b-cloud (~13 s). gpt-oss:120b refuses images.
# ============================================================================

VISION_MODELS = ["minimax-m3", "gemma4:31b-cloud", "gemma4:31b", "minimax-m2.7"]
DEFAULT_VISION = "minimax-m3"
FRAMES_PER_CALL = 6      # small batches keep the model accurate per picture
FRAME_WIDTH = 512        # enough to describe a scene
WORKERS = 5              # batches in flight; more makes Ollama drop some

# "target" pictures on ordinary clips, "lo"/"hi" seconds between pictures,
# "cap" the most it will ever take so a three-hour video still finishes.
DETAIL_LEVELS = {
    "quick":  {"target": 10, "lo": 6.0, "hi": 240.0, "cap": 110},
    "normal": {"target": 24, "lo": 3.0, "hi": 120.0, "cap": 240},
    "fine":   {"target": 48, "lo": 1.5, "hi": 45.0,  "cap": 460},
}


def pick_interval(seconds, detail="normal"):
    seconds = max(1.0, float(seconds or 0))
    lv = DETAIL_LEVELS.get(detail) or DETAIL_LEVELS["normal"]
    step = max(lv["lo"], min(lv["hi"], seconds / lv["target"]))
    if seconds / step > lv["cap"]:
        step = seconds / lv["cap"]
    return step


def mmss_safe(t):
    t = int(t or 0)
    return "%d:%02d" % (t // 60, t % 60)


def frames_b64(url, start, end, interval, width, note=None):
    """Stills every `interval` seconds, near-duplicates dropped (no point
    paying to describe the same picture twice). Returns (frames, error)."""
    vs = host()
    try:
        info = vs.video_info(url)
        dur = float(info.get("duration") or end or 0)
        times, t = [], float(start)
        while t < end:
            times.append(t)
            t += interval
        if note:
            note("Taking %d pictures…" % len(times))
        got, problems = vs.raw_frames(url, times, dur, width=width)
    except Exception as e:
        return [], "Could not open that video (%s)" % (vs.explain_failure(e) or str(e)[:150])
    frames, prev = [], None
    for t, p in got:
        h = vs.dhash(p)
        if prev is not None and bin(h ^ prev).count("1") <= 4:
            continue
        prev = h
        with open(p, "rb") as f:
            frames.append({"t": t, "path": p, "b64": base64.b64encode(f.read()).decode()})
    if not frames:
        return [], "No pictures could be taken. " + "; ".join(problems[:2])
    return frames, None


WATCH_SYSTEM = """You are describing frames taken from a video, in order.

For EACH picture, write one line covering:
- ANY TEXT VISIBLE ON SCREEN, copied EXACTLY, in quotes. Names, captions, \
titles, subtitles, signs, labels, logos, numbers, dates, scoreboards. This is \
the most important part: copy it letter for letter, do not tidy it up.
- Who is there and what they are doing.
- Where it is, and anything that identifies it.
- Say "scene change" when the shot clearly changes.

Rules:
- Describe only what you can SEE. Never guess what is being said.
- Never invent. If a picture is unclear, say "unclear".
- No opinions, no atmosphere, no storytelling. Facts only.
- One line per picture, in the same order, numbered exactly as given."""


def describe_batch(model, key, batch):
    listing = "\n".join("Picture %d at %s" % (i + 1, mmss_safe(f["t"])) for i, f in enumerate(batch))
    payload = {"model": model, "stream": False, "options": {"temperature": 0.1},
               "messages": [{"role": "system", "content": WATCH_SYSTEM},
                            {"role": "user", "images": [f["b64"] for f in batch],
                             "content": "Describe these %d pictures, one line each, numbered 1 to %d.\n\n%s"
                                        % (len(batch), len(batch), listing)}]}
    data, err = http_json(CHAT_URL, payload, key)
    if err:
        return None, err
    lines = [l.strip() for l in ((data or {}).get("message") or {}).get("content", "").splitlines() if l.strip()]
    out = {}
    for l in lines:
        m = re.match(r"^\**\s*(?:Picture\s*)?(\d+)\s*[\.\)\:\-—]\s*(.+)$", l)
        if m:
            out[int(m.group(1))] = m.group(2).strip(" *")
    if not out:
        out = {i: l for i, l in enumerate(lines[:len(batch)], 1)}
    return [{"t": f["t"], "seen": out.get(i + 1, "")} for i, f in enumerate(batch)], None


VERIFY_SYSTEM = """You are checking a speech-to-text transcript against what is \
actually VISIBLE in the same video.

The transcript was produced from audio alone, so its errors are MIS-HEARINGS. \
The observations were read off the screen, so any text quoted in them is the \
correct spelling.

Find places where the transcript disagrees with what is on screen, and correct \
the transcript. Names, places, brands, technical terms, numbers and dates \
especially.

Rules you must follow exactly:
- Only correct something when the on-screen text or scene supports it.
- The corrected version must plausibly SOUND like what the transcript says.
- Never rewrite for style, never shorten, never summarise, never translate.
- Never invent. If nothing disagrees, return an empty list.

Return ONLY a JSON object of this exact shape:
{"fixes":[{"from":"<exact text from the transcript>","to":"<corrected text>",\
"why":"<what on screen shows this>","confidence":"high|medium|low"}]}

"from" MUST be copied character-for-character from the transcript and be long \
enough to appear only once."""


def verify_against_screen(text, seen, model, key, note=None):
    screen = "\n".join("[%s] %s" % (s["at"], s["seen"]) for s in seen)
    fixes, problems, parts = [], [], chunks(text)
    for i, part in enumerate(parts, 1):
        if note:
            note("Checking the words against the screen… %d of %d" % (i, len(parts)))
        out, err = chat(model, VERIFY_SYSTEM,
                        "WHAT IS VISIBLE ON SCREEN (read from the video)\n" + screen[:24000]
                        + "\n\nTRANSCRIPT PART %d (from the audio)\n%s" % (i, part), key, want_json=True)
        if err:
            problems.append(err)
            continue
        fixes += keep_fixes(parse_json(out), text)
    return fixes, problems


# ---------------------------------------------------------------- jobs -----
JOBS = {}
_jobs_lock = threading.Lock()


def set_job(jid, **kw):
    with _jobs_lock:
        JOBS.setdefault(jid, {}).update(kw)


def run_batches(fn, batches, jid, label):
    """Run batches a few at a time, then retry the failed ones slowly: firing
    many at once makes Ollama drop some, and a gap is where a name hides."""
    results, problems, failed, n = [], [], [], {"done": 0}

    def work(b):
        got, e = fn(b)
        n["done"] += 1
        set_job(jid, done=n["done"], message="%s… %d of %d" % (label, n["done"], len(batches)))
        return b, got, e

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for b, got, e in pool.map(work, batches):
            if e:
                failed.append(b)
                problems.append(e)
            elif got:
                results.extend(got)
    if failed:
        set_job(jid, message="Retrying %d part%s that did not come back…" % (len(failed), "" if len(failed) == 1 else "s"))
        for b in failed:
            got, e = fn(b)
            if got:
                results.extend(got)
            time.sleep(0.4)
    return results, problems


def run_watch_job(jid, url, start, end, vmodel, tmodel, detail, text):
    vs = host()
    note = lambda m: set_job(jid, message=m)
    ckey = "%s|%s|%s|%s|%s|%s|%s" % (url, start, end, vmodel, tmodel, detail,
                                      hashlib.sha1(text.encode()).hexdigest())
    try:
        hit = vs.cache_get("watch", ckey)
        if hit:
            set_job(jid, cached=True, **hit)
            return
        key = api_key()
        interval = pick_interval(end - start, detail)
        note("Opening the video…")
        frames, err = frames_b64(url, start, end, interval, FRAME_WIDTH, note)
        if err:
            set_job(jid, state="error", error=err)
            return
        batches = [frames[i:i + FRAMES_PER_CALL] for i in range(0, len(frames), FRAMES_PER_CALL)]
        set_job(jid, state="watching", total=len(batches), done=0,
                message="Looking at %d moments…" % len(frames),
                image_tokens=sum(common.image_tokens(*vs.img_size(f["path"])) for f in frames))
        results, problems = run_batches(lambda b: describe_batch(vmodel, key, b), batches, jid,
                                        "Looking at the video")
        if not results:
            msg = problems[0] if problems else "The model returned nothing."
            if "IMAGE_UNSUPPORTED" in msg or "image input" in msg:
                msg = ("%s cannot look at pictures. Choose one that can: %s."
                       % (vmodel, ", ".join(VISION_MODELS[:3])))
            set_job(jid, state="error", error=msg)
            return
        results.sort(key=lambda r: r["t"])
        seen = [{"t": r["t"], "at": mmss_safe(r["t"]), "seen": r["seen"]} for r in results if r["seen"]]
        fixes = []
        if text.strip():
            set_job(jid, state="checking", message="Checking every name and detail against the screen…")
            fixes, vp = verify_against_screen(text, seen, tmodel or vmodel, key, note)
            problems += vp
        final = {"state": "done", "seen": seen, "fixes": fixes, "problems": problems[:3],
                 "interval": round(interval, 1), "count": len(seen), "planned": len(frames),
                 "message": "Watched %d of %d moments" % (len(seen), len(frames))}
        if len(seen) == len(frames):
            vs.cache_put("watch", ckey, final)
        set_job(jid, **final)
    except Exception as e:
        set_job(jid, state="error", error="Something went wrong: %s" % (str(e)[:200],))


def _start(body, target, extra):
    url = host().normalise_url(body.get("url") or "")
    c = cfg()
    vmodel = (body.get("model") or c.get("vision_model") or DEFAULT_VISION).strip()
    tmodel = (body.get("text_model") or c.get("model") or "").strip()
    if not url:
        return jsonify({"error": "Find a video first."}), 400
    try:
        start, end = float(body.get("start") or 0), float(body.get("end") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "That time range does not make sense."}), 400
    if end <= start and not os.path.exists(url):
        return jsonify({"error": "Could not tell how long the video is."}), 400
    jid = uuid.uuid4().hex[:16]
    set_job(jid, state="starting", message="Starting…", done=0, total=0, seen=[], fixes=[],
            problems=[], fragments=[], prompt="")
    threading.Thread(target=target, args=(jid, url, start, end, vmodel, tmodel) + extra,
                     daemon=True).start()
    return jsonify({"job": jid})


@bp.route("/api/ai/watch/start", methods=["POST"])
def api_watch_start():
    body = request.get_json(silent=True) or {}
    if not api_key():
        return jsonify({"error": NO_KEY}), 400
    detail = body.get("detail") if body.get("detail") in DETAIL_LEVELS else "normal"
    return _start(body, run_watch_job, (detail, body.get("text") or ""))


# ============================================================================
#  READING A PROMPT OFF THE SCREEN
#  Tutorials often show a prompt on screen without sharing it. It is rarely
#  fully visible in one frame, so text is read from MANY frames and the
#  overlapping pieces are joined. Gaps stay marked as gaps, never filled in.
# ============================================================================

TEXT_WIDTH = 1152        # text at 512 px is unreadable

READ_SYSTEM = """You copy text out of pictures. Nothing else.

For EACH picture: if it shows typed or written text (a prompt, a chat message, \
code, a caption, a document, a settings panel) copy that text EXACTLY as it \
appears. Letter for letter. Keep the line breaks, the punctuation, the odd \
capitals, the typos.

If a picture shows NO readable text, write exactly: NONE

Rules:
- Never translate, never correct, never tidy, never summarise.
- Never continue a sentence that is cut off. If the text is clipped at the edge \
or covered, write it up to where it stops and put [...] there.
- Do not describe the picture. Do not mention the interface. Only the text.
- One block per picture, each starting "### <number>" on its own line."""

ASSEMBLE_SYSTEM = """You are rebuilding ONE piece of text from fragments read \
off a video at different moments. The same text was on screen more than once, \
scrolled, or was partly covered, so the fragments overlap and repeat.

Join them into the single most complete version you can.

Rules you must follow exactly:
- Use ONLY words that appear in the fragments. Invent nothing.
- Where fragments overlap, merge them; do not repeat the overlap.
- Where something is clearly missing between two pieces, put [...] on its own.
- Keep the original wording, spelling and line breaks. Do not improve it.
- If the fragments are plainly several different texts, return the longest \
coherent one and list the others after it.

Return ONLY a JSON object:
{"prompt":"<the rebuilt text>","complete":"yes|partly|no",
 "note":"<one short line on what is missing or uncertain>"}"""


def read_text_batch(model, key, batch):
    listing = "\n".join("Picture %d at %s" % (i + 1, mmss_safe(f["t"])) for i, f in enumerate(batch))
    payload = {"model": model, "stream": False, "options": {"temperature": 0},
               "messages": [{"role": "system", "content": READ_SYSTEM},
                            {"role": "user", "images": [f["b64"] for f in batch],
                             "content": "Copy the text from these %d pictures.\n\n%s" % (len(batch), listing)}]}
    data, err = http_json(CHAT_URL, payload, key)
    if err:
        return None, err
    text = ((data or {}).get("message") or {}).get("content", "") or ""
    blocks, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^\s*#{2,}\s*(\d+)\s*$", line)
        if m:
            cur = int(m.group(1))
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(line)
    if not blocks:
        blocks = {1: text.splitlines()}
    out = []
    for i, f in enumerate(batch):
        body = "\n".join(blocks.get(i + 1, [])).strip()
        if body and body.upper() not in ("NONE", "NONE.", "[NONE]"):
            out.append({"t": f["t"], "at": mmss_safe(f["t"]), "text": body})
    return out, None


def join_locally(pieces):
    """No model: keep every distinct line once, in the order first seen."""
    seen, out = set(), []
    for p in pieces:
        for line in p["text"].splitlines():
            k = re.sub(r"\W+", "", line.lower())
            if k and k not in seen:
                seen.add(k)
                out.append(line)
    return "\n".join(out)


def run_prompt_job(jid, url, start, end, vmodel, tmodel, spoken_text):
    vs = host()
    note = lambda m: set_job(jid, message=m)
    ckey = "%s|%s|%s|%s|%s" % (url, start, end, vmodel, bool(api_key()))
    try:
        hit = vs.cache_get("prompt", ckey)
        if hit:
            set_job(jid, cached=True, **hit)
            return
        key = api_key()
        tried, caption, why = [], "", ""
        local = vs.local_images(url) if os.path.exists(url) else []
        photo = False
        if local:
            got = [(float(i), p) for i, p in enumerate(local)]
            tried.append("%d pictures from this PC" % len(got))
        else:
            info = vs.video_info(url)
            caption = (info.get("description") or "").strip()   # free, exact text
            tried.append("the post's own caption")
            if info.get("photo_post"):
                photo = True
                got = [(0.0, p) for p in vs.photo_post_images(url, info)]
                tried.append("the photo post's first slide")
            else:
                span = max(1.0, end - start)
                step = 1.0 if span <= 90 else 2.0 if span <= 600 else max(3.0, span / 260.0)
                times, t = [], float(start)
                while t < end:
                    times.append(t)
                    t += step
                note("Taking %d pictures…" % len(times))
                got, _p = vs.raw_frames(url, times, float(info.get("duration") or end), width=TEXT_WIDTH)
        # 1) read every picture on this PC (free)
        note("Reading the screen on this PC…")
        texts = vs.ocr_images([p for _, p in got])
        found, unread = [], []
        for t, p in got:
            txt = texts.get(p, "")
            if len(txt) >= 25:
                found.append({"t": t, "at": mmss_safe(t), "text": txt})
            elif txt:                         # something is there but OCR lost it
                unread.append((t, p))
        tried.append("local OCR (%s): text in %d of %d pictures" % (vs.ocr_engine(), len(found), len(got)))
        if not vs.ocr_engine():
            unread = list(got)
        # 2) only what OCR could not read goes to the vision model
        if unread and key:
            batch_in = []
            for t, p in unread:
                with open(p, "rb") as f:
                    batch_in.append({"t": t, "b64": base64.b64encode(f.read()).decode()})
            batches = [batch_in[i:i + 4] for i in range(0, len(batch_in), 4)]
            set_job(jid, state="reading", total=len(batches), done=0)
            more, problems = run_batches(lambda b: read_text_batch(vmodel, key, b), batches, jid,
                                         "Reading hard parts with %s" % vmodel)
            found += [m for m in more if len(m["text"]) >= 25]
            tried.append("vision model on %d hard pictures" % len(unread))
        elif unread:
            tried.append("%d pictures had text OCR could not read (no Ollama key to try harder)" % len(unread))
        found.sort(key=lambda f: f["t"])
        if len(caption) >= 25:
            found.insert(0, {"t": -1.0, "at": "caption", "text": caption})
        if not found:
            msg = ("No text could be read off the screen, and the post has no caption. "
                   "If the prompt is only spoken, the transcript has it.")
            final = {"state": "done", "prompt": "", "complete": "no", "note": msg,
                     "message": msg, "tried": tried, "fragments": []}
            set_job(jid, **final)
            return
        prompt, complete = "", "partly"
        if key:
            set_job(jid, state="assembling", message="Joining %d pieces of text…" % len(found))
            blob = "\n\n".join(("--- the post's own caption (exact text, trust this most) ---\n%s" % f["text"])
                               if f["at"] == "caption" else "--- seen at %s ---\n%s" % (f["at"], f["text"])
                               for f in found)[:40000]
            user = "FRAGMENTS READ OFF THE SCREEN\n" + blob
            if spoken_text.strip():
                user += ("\n\nWHAT WAS SAID OUT LOUD (use it only to settle unclear words)\n"
                         + spoken_text[:8000])
            out, err = chat(tmodel or vmodel, ASSEMBLE_SYSTEM, user, key, want_json=True)
            parsed = parse_json(out) if out else {}
            prompt = (parsed.get("prompt") or "").strip()
            complete = parsed.get("complete") or "partly"
            why = (parsed.get("note") or "").strip()
        if not prompt:
            prompt = join_locally(found)
            complete = "partly"
            why = ("Joined on this PC without a model: every distinct line once, in order. "
                   "Check the order and the gaps.")
        if photo:
            why = (PHOTO + " " + why).strip()
            complete = "partly" if complete == "yes" else complete
        final = {"state": "done", "prompt": prompt, "complete": complete, "note": why,
                 "fragments": found, "tried": tried, "slideshow": photo,
                 "message": "Read %d pieces of text" % len(found)}
        vs.cache_put("prompt", ckey, final)
        set_job(jid, **final)
    except Exception as e:
        set_job(jid, state="error", error="Something went wrong: %s" % (str(e)[:200],))


PHOTO = ("Photo post: only the first slide and the caption are available to anyone "
         "logged out; later slides cannot be fetched.")


@bp.route("/api/ai/prompt/start", methods=["POST"])
def api_prompt_start():
    """Works without a key too: local OCR plus a plain join."""
    body = request.get_json(silent=True) or {}
    return _start(body, run_prompt_job, (body.get("text") or "",))


@bp.route("/api/ai/watch/status")
def api_watch_status():
    jid = request.args.get("job", "")
    with _jobs_lock:
        job = dict(JOBS.get(jid) or {})
        if job.get("state") in ("done", "error"):
            JOBS.pop(jid, None)          # hand it over once, then forget it
    if not job:
        return jsonify({"error": "That job is not running any more."}), 404
    return jsonify(job)


def register(app):
    app.register_blueprint(bp)
    return True
