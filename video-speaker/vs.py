"""Video Speaker command line: lets an AI look inside a video cheaply.

Cheapest first. Stop at the first step that answers the question:
    vs.py info       <url>                 title, length, description (often the answer)
    vs.py transcript <url> [--brief]       what was said (site subtitles, else Whisper)
    vs.py text       <url>                 words on screen, read on this PC (OCR)
    vs.py sheet      <url> [--grid 3x3]    ONE picture with many moments
    vs.py frames     <url> [--at 1:30]     single small pictures, --crop for close-ups
    vs.py watch      <url>                 a vision model describes it (Ollama credits)
    vs.py prompt     <url|folder>          rebuild a prompt shown on screen
    vs.py done                             delete the pictures now (they also expire)
    vs.py serve | status                   start the server / show its state

Every command ends with a rough token count of what it printed. Pictures and
videos live only in a temp folder and are deleted automatically.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get("VIDEOSPEAKER_APP") or os.path.join(HERE, "app")
sys.path.insert(0, APP_DIR)
import common                                            # noqa: E402  (stdlib only)

BASE = common.BASE
OUT = []                                                 # what was printed, for the token count
IMG_TOKENS = [0]


def say(s=""):
    OUT.append(s)
    print(s)


def note(s):
    print(s, file=sys.stderr, flush=True)                # progress: not part of the answer


def die(msg):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


# ----------------------------------------------------------------- server ---

def health(timeout=2):
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError:
        return {"old": True}
    except Exception:
        return None


def ensure_server():
    h = health()
    if h and h.get("ok"):
        return h
    if h and h.get("old"):
        die("Port %d is used by an older Video Speaker. Close it, or use another port: "
            "set VIDEOSPEAKER_PORT=8766" % common.PORT)
    server = os.path.join(APP_DIR, "server.py")
    if not os.path.exists(server):
        die("Video Speaker's app folder is missing: %s\nDownload Video Speaker again." % APP_DIR)
    note("Starting Video Speaker…")
    flags = 0
    if common.IS_WIN:
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                 | 0x01000000)                           # CREATE_BREAKAWAY_FROM_JOB
    cmd = [common.server_python(), server]
    os.makedirs(common.DATA_DIR, exist_ok=True)
    log_file = os.path.join(common.DATA_DIR, "server.log")   # so the server's errors are not lost
    if os.path.exists(log_file) and os.path.getsize(log_file) > 1_000_000:
        os.remove(log_file)
    kw = dict(cwd=APP_DIR, stdin=subprocess.DEVNULL, stdout=open(log_file, "a", encoding="utf-8"),
              stderr=subprocess.STDOUT, close_fds=True)
    try:
        subprocess.Popen(cmd, creationflags=flags, **kw)
    except OSError:                                      # job does not allow breakaway
        subprocess.Popen(cmd, creationflags=flags & ~0x01000000, **kw)
    t0 = time.time()
    while time.time() - t0 < 45:
        time.sleep(0.3)
        h = health(1)
        if h and h.get("ok"):
            note("Ready in %.1f s." % (time.time() - t0))
            return h
    die("Video Speaker did not start within 45 s. Its log is here:\n  %s"
        % os.path.join(common.DATA_DIR, "server.log"))


def call(path, body=None, timeout=600):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            j = {}
        msg = j.get("error") or "Video Speaker replied %s." % e.code
        if j.get("tried"):
            msg += "\nTried: " + "; ".join(j["tried"])
        die(msg)


def q(url):
    if os.path.exists(url):
        url = os.path.abspath(url)
    return urllib.parse.quote(url, safe="")


def mmss(t):
    t = int(t or 0)
    return ("%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600
            else "%d:%02d" % (t // 60, t % 60))


def parse_time(s):
    """90, 1:30 or 1:02:03."""
    try:
        out = 0.0
        for part in str(s).strip().split(":"):
            out = out * 60 + float(part or 0)
        return out
    except ValueError:
        die("'%s' is not a time. Use seconds (90), or 1:30, or 1:02:03." % s)


def info_of(url):
    return call("/api/info?plain=1&url=" + q(url))


# --------------------------------------------------------------- commands ---

def cmd_info(a):
    d = info_of(a.url)
    say("Title      : %s" % d.get("title"))
    say("Channel    : %s" % d.get("uploader"))
    say("Site       : %s" % d.get("site"))
    if d.get("duration"):
        say("Length     : %s" % mmss(d["duration"]))
    if d.get("upload_date"):
        say("Published  : %s" % d["upload_date"])
    if d.get("views") is not None:
        say("Views      : %s" % d["views"])
    if d.get("sub_langs"):
        say("Subtitles  : %s" % ", ".join(d["sub_langs"][:10]))
    elif d.get("auto_langs"):
        say("Subtitles  : auto-captions only (%s)" % ("en" if "en" in d["auto_langs"] else d["auto_langs"][0]))
    if d.get("photo_post"):
        say("Kind       : photo post (slideshow), not a video. Only slide 1 can be fetched.")
    if d.get("partial"):
        say("PARTIAL    : %s" % d.get("reason"))
        say("Tried      : %s" % "; ".join(d.get("tried") or []))
        say("Missing    : %s" % ", ".join(d.get("missing") or []))
    desc = (d.get("description") or "").strip()
    if desc:
        cap = 3000 if not a.all else 100000
        say("\nDescription:\n%s" % desc[:cap])
        if len(desc) > cap:
            say("[... %d more characters: add --all]" % (len(desc) - cap))


FILLER = __import__("re").compile(r"(?i)\b(?:um+|uh+|erm+|uhm+|hmm+)\b[,.]?\s*")


def tidy(segs):
    """Rolling auto-captions repeat each line two or three times, and filler
    sounds carry nothing. Drop both; keep every real word."""
    import re
    out = []
    for s in segs:
        text = " ".join((s.get("text") or "").split())
        text = FILLER.sub("", text).strip()
        if not text or re.fullmatch(r"\[[^\]]*\]", text) and out and out[-1]["text"] == text:
            continue
        if out and (text == out[-1]["text"] or out[-1]["text"].endswith(text)):
            continue
        if out and text.startswith(out[-1]["text"]):      # the line grew: keep the longer one
            out[-1] = {"t": out[-1]["t"], "text": text}
            continue
        out.append({"t": s.get("t", 0), "text": text})
    return out


def cmd_transcript(a):
    path = "/api/transcript?url=" + q(a.url)
    if a.start:
        path += "&start=%s" % parse_time(a.start)
    if a.end:
        path += "&end=%s" % parse_time(a.end)
    if a.rich:
        path += "&rich=1"
    if a.whisper:
        path += "&mode=listen"
    note("Getting the transcript (site subtitles first, Whisper only if there are none)…")
    d = call(path, timeout=3600)
    segs = d.get("segments") or []
    if not segs:
        die("No spoken text came back for that range. Tried: %s" % "; ".join(d.get("tried") or []))
    say("# source: %s%s" % (d.get("source"), " (%s)" % d["lang"] if d.get("lang") and d.get("source") != "whisper" else ""))
    if d.get("rich"):
        body = d["rich"]
    else:
        rows = tidy(segs)
        if a.brief:
            # one line per minute: an outline to decide which range to read
            minutes = {}
            for r in rows:
                minutes.setdefault(int(r["t"] // 60), []).append(r["text"])
            body = "\n".join("[%s] %s…" % (mmss(m * 60), " ".join(t)[:140])
                             for m, t in sorted(minutes.items()))
            say("# brief: one line per minute. Read a part with --start/--end.")
        elif a.times:
            body = "\n".join("[%s] %s" % (mmss(r["t"]), r["text"]) for r in rows)
        else:
            body = " ".join(r["text"] for r in rows)
    cap = 10 ** 9 if a.all else a.max_chars
    say(body[:cap])
    if len(body) > cap:
        last = body[:cap].rsplit("[", 1)[-1][:8] if a.times else ""
        say("\n[... %d more characters (~%d tokens) not shown. Ask with --start <time>%s or --all]"
            % (len(body) - cap, (len(body) - cap) // 4, " (last shown: %s)" % last if last else ""))


def show_pictures(frames, label="picture"):
    total = 0
    for f in frames:
        extra = ""
        if f.get("kind") == "changed-area":
            extra = "  only the changed area %s" % f.get("box")
        elif f.get("kind") == "slide":
            extra = "  (slide)"
        say("  %-8s %s  %dx%d ~%d tok%s" % (f.get("at", ""), f["path"], f["w"], f["h"], f["tokens"], extra))
        total += f["tokens"]
    IMG_TOKENS[0] += total
    say("Open these files to see them (~%d tokens for %d %s%s). Run `vs.py done` when finished."
        % (total, len(frames), label, "" if len(frames) == 1 else "s"))


def cmd_sheet(a):
    note("Making a contact sheet…")
    path = "/api/sheet?url=%s&grid=%s&cell=%d" % (q(a.url), a.grid, a.cell)
    if a.start:
        path += "&start=%s" % parse_time(a.start)
    if a.end:
        path += "&end=%s" % parse_time(a.end)
    d = call(path)
    if d.get("note"):
        say("# " + d["note"])
    if d.get("frames"):
        return show_pictures(d["frames"], "slide")
    s = d["sheet"]
    say("# %s grid, moments: %s" % (s["grid"], ", ".join(c["at"] for c in s["cells"])))
    say("# Zoom in on one moment: vs.py frames <url> --at <time> [--crop x,y,w,h --size 1024]")
    show_pictures([dict(s, at="sheet")], "sheet")
    for p in d.get("problems") or []:
        say("# could not take: " + p)


def cmd_frames(a):
    path = "/api/frames?url=%s&n=%d&width=%d" % (q(a.url), a.n, a.size)
    if a.at:
        path += "&at=" + ",".join(str(parse_time(x)) for x in a.at.split(",") if x.strip())
    if a.start:
        path += "&start=%s" % parse_time(a.start)
    if a.end:
        path += "&end=%s" % parse_time(a.end)
    if a.crop:
        path += "&crop=" + a.crop
    if a.changes:
        path += "&changes=1"
    if a.even:
        path += "&scene=0"
    note("Taking pictures…")
    d = call(path)
    if d.get("note"):
        say("# " + d["note"])
    fr = d.get("frames") or []
    if not fr:
        die("No pictures could be taken. %s" % "; ".join(d.get("problems") or []))
    show_pictures(fr)
    if d.get("dropped"):
        say("# %d near-identical picture%s skipped (nothing new on screen)."
            % (d["dropped"], "" if d["dropped"] == 1 else "s"))
    for p in d.get("problems") or []:
        say("# could not take: " + p)
    if d.get("partial"):
        say("# PARTIAL: not every requested moment could be taken.")


def cmd_text(a):
    path = "/api/text?url=" + q(a.url)
    if a.n:
        path += "&n=%d" % a.n
    if a.start:
        path += "&start=%s" % parse_time(a.start)
    if a.end:
        path += "&end=%s" % parse_time(a.end)
    note("Reading the words on screen on this PC…")
    d = call(path, timeout=1200)
    if d.get("error"):
        die(d["error"] + " Try `vs.py sheet` and look yourself, or `vs.py prompt`.")
    if d.get("note"):
        say("# " + d["note"])
    items = d.get("items") or []
    say("# on-screen text, local OCR (%s), looked at %d moments%s"
        % (d.get("engine"), d.get("looked", 0), ", from cache" if d.get("cached") else ""))
    if not items:
        say("No readable text on screen.")
    body, shown = "", 0
    for it in items:
        block = "[%s]\n%s\n" % (it["at"], it["text"])
        if len(body) + len(block) > a.max_chars and not a.all:
            break
        body += block
        shown += 1
    say(body.rstrip())
    if shown < len(items):
        say("[... %d more moments with text. Use --start/--end or --all]" % (len(items) - shown))
    if d.get("unreadable") and items:
        say("# %d moments had no readable text." % len(d["unreadable"]))


def poll(job, label):
    last, t0 = "", time.time()
    while True:
        s = call("/api/ai/watch/status?job=" + job, timeout=120)
        msg = s.get("message") or ""
        if msg != last:
            note("  [%4.0fs] %s" % (time.time() - t0, msg))
            last = msg
        if s.get("state") == "error":
            die(s.get("error") or "%s failed." % label)
        if s.get("state") == "done":
            return s
        if time.time() - t0 > 3600:
            die("%s took over an hour; stopped waiting." % label)
        time.sleep(1.5)


def cmd_watch(a):
    d = info_of(a.url)
    dur = float(d.get("duration") or 0)
    if dur <= 0:
        die("Could not tell how long the video is. Try `vs.py sheet` instead.")
    j = call("/api/ai/watch/start", {"url": a.url, "start": 0, "end": dur,
                                     "detail": a.detail, "text": a.text or ""})
    s = poll(j["job"], "Watching")
    say("# What is on screen: %s (%s), %d moments%s"
        % (d.get("title"), mmss(dur), s.get("count") or 0, ", from cache" if s.get("cached") else ""))
    if s.get("planned") and s.get("count", 0) < s["planned"]:
        say("# PARTIAL: %d of %d moments came back." % (s["count"], s["planned"]))
    for f in s.get("seen") or []:
        say("[%s] %s" % (f["at"], f["seen"]))
    if s.get("fixes"):
        say("\n# Transcript corrections suggested from what was on screen")
        for f in s["fixes"]:
            say("  %-30s -> %-30s (%s) %s" % (f["from"][:30], f["to"][:30], f["confidence"], f["why"][:70]))


def cmd_prompt(a):
    url = os.path.abspath(a.url) if os.path.exists(a.url) else a.url
    body = {"url": url, "start": 0, "end": 0, "text": a.text or ""}
    if not os.path.exists(a.url):
        body["end"] = float(info_of(a.url).get("duration") or 0)
    j = call("/api/ai/prompt/start", body)
    s = poll(j["job"], "Reading the screen")
    say("# Recovered text, complete: %s%s" % (s.get("complete"), ", from cache" if s.get("cached") else ""))
    if s.get("note"):
        say("# " + s["note"])
    if s.get("tried"):
        say("# tried: " + "; ".join(s["tried"]))
    if not s.get("prompt"):
        die(s.get("note") or "Nothing readable was found.")
    say()
    say(s["prompt"])


def cmd_done(a):
    call("/api/done", {})
    say("Pictures deleted.")


def cmd_status(a):
    h = health()
    if not h or not h.get("ok"):
        say("Video Speaker is not running (it starts by itself on first use).")
        return
    for k in ("version", "port", "ffmpeg", "ocr", "ai", "media_dir", "data_dir"):
        say("%-10s %s" % (k, h.get(k)))


def cmd_serve(a):
    h = ensure_server()
    say("Video Speaker %s is running at %s" % (h.get("version"), BASE))


# ------------------------------------------------------------------- main ---

def main():
    p = argparse.ArgumentParser(prog="vs.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, url=True):
        s = sub.add_parser(name)
        if url:
            s.add_argument("url")
        s.set_defaults(fn=fn)
        return s

    def ranged(s):
        s.add_argument("--start")
        s.add_argument("--end")

    s = add("info", cmd_info)
    s.add_argument("--all", action="store_true", help="the whole description")

    s = add("transcript", cmd_transcript)
    ranged(s)
    s.add_argument("--brief", action="store_true", help="one line per minute")
    s.add_argument("--times", action="store_true", help="a timestamp on every line")
    s.add_argument("--rich", action="store_true", help="timestamps, speaker guesses, pauses")
    s.add_argument("--whisper", action="store_true", help="ignore subtitles, listen with Whisper")
    s.add_argument("--max-chars", type=int, default=12000)
    s.add_argument("--all", action="store_true")

    s = add("text", cmd_text)
    ranged(s)
    s.add_argument("--n", type=int, default=0, help="how many moments to read")
    s.add_argument("--max-chars", type=int, default=8000)
    s.add_argument("--all", action="store_true")

    s = add("sheet", cmd_sheet)
    ranged(s)
    s.add_argument("--grid", default="3x3")
    s.add_argument("--cell", type=int, default=320, help="width of each cell in pixels")

    s = add("frames", cmd_frames)
    ranged(s)
    s.add_argument("--at", help="times, e.g. 0,1:30,4:05")
    s.add_argument("--n", type=int, default=6, help="how many when spreading")
    s.add_argument("--size", type=int, default=512, help="width in pixels")
    s.add_argument("--crop", help="x,y,w,h as fractions, e.g. 0,0.7,1,0.3 = bottom 30%%")
    s.add_argument("--changes", action="store_true", help="send only the area that changed")
    s.add_argument("--even", action="store_true", help="even spacing, not scene changes")

    s = add("watch", cmd_watch)
    s.add_argument("--detail", default="quick", choices=["quick", "normal", "fine"])
    s.add_argument("--text", help="transcript to check against what is seen")

    s = add("prompt", cmd_prompt)
    s.add_argument("--text", help="what was said, to settle unclear words")

    add("done", cmd_done, url=False)
    add("status", cmd_status, url=False)
    add("serve", cmd_serve, url=False)

    a = p.parse_args()
    if a.cmd != "status":
        ensure_server()
    a.fn(a)
    txt = common.text_tokens("\n".join(OUT))
    if IMG_TOKENS[0]:
        print("# ~%d tokens of text + ~%d if every picture is opened" % (txt, IMG_TOKENS[0]))
    else:
        print("# ~%d tokens" % txt)


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        die("Stopped.")
    except urllib.error.URLError as e:
        die("Could not reach Video Speaker at %s (%s)" % (BASE, e.reason))
    except Exception as e:                       # a stack trace helps nobody read a video
        die("%s: %s" % (e.__class__.__name__, str(e)[:300]))
