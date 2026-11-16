"""
ClipBot - 24/7 live stream clipper.
Telegram + private web admin panel + /health endpoint.

Flow: live link -> rolling 2s segment buffer (yt-dlp + ffmpeg) ->
  trigger (chat command / Whisper keyword / manual) -> last N seconds cut ->
  sent to Telegram -> file deleted.
"""
import glob
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import requests
from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session)
from jinja2 import DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# paths / logging
# --------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR") or (
    "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK)
    else os.path.abspath("./data"))
os.makedirs(DATA_DIR, exist_ok=True)
BUF_ROOT = "/tmp/clipbot_buf"
OUT_DIR = "/tmp/clipbot_out"
COOKIE_FILE = "/tmp/clipbot_cookies.txt"
os.makedirs(BUF_ROOT, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

LOGS = deque(maxlen=200)
log = logging.getLogger("clipbot")
log.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_sh)


def scrub(x):
    """Remove secrets (bot token / api keys) from any text before logging."""
    s = str(x)
    for k in ("telegram_token", "groq_api_key", "youtube_api_key"):
        v = S.d.get(k) if "S" in globals() else ""
        if v:
            s = s.replace(v, "***")
    return s


class _Ring(logging.Handler):
    def emit(self, record):
        LOGS.append(f"{datetime.now():%H:%M:%S} {scrub(record.getMessage())}")


log.addHandler(_Ring())

# --------------------------------------------------------------------------
# settings (everything editable from the admin panel)
# --------------------------------------------------------------------------
DEFAULTS = {
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
    "youtube_api_key": os.environ.get("YOUTUBE_API_KEY", ""),
    "clip_chat_id": "",          # optional: send clips to this chat/channel instead of owners
    "owner_chat_ids": [],
    "bot_name": "bot",
    "clip_seconds": 15,
    "buffer_seconds": 90,
    "cooldown_seconds": 20,
    "max_height": 720,
    "chat_enabled": True,
    "chat_who": "all",           # all | mods_members
    "audio_enabled": True,
    "audio_chunk_seconds": 15,
    "audio_daily_limit": 1500,
    "audio_language": "",
    "keywords": "kill, kills, paani, pani, headshot, clutch, पानी, किल",
    "max_streams": 2,
    "keepalive": True,
    "cookies": "",
    "ytdlp_extra_args": "",
    "active_links": [],
    "admin_password_hash": "",
    "secret_key": "",
}


class Settings:
    def __init__(self):
        self.path = os.path.join(DATA_DIR, "settings.json")
        self.lock = threading.RLock()
        self.d = dict(DEFAULTS)
        try:
            with open(self.path) as f:
                self.d.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("settings load error: %s", e)

    def get(self, k):
        return self.d.get(k, DEFAULTS.get(k))

    def update(self, **kw):
        with self.lock:
            self.d.update(kw)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.d, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)


S = Settings()
if not S.get("secret_key"):
    S.update(secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32))


def check_pw(pw):
    h = S.get("admin_password_hash")
    if h:
        return check_password_hash(h, pw)
    env = os.environ.get("ADMIN_PASSWORD", "")
    return bool(env) and hmac.compare_digest(pw.encode(), env.encode())


# brute-force lock (per IP / per telegram chat)
FAILS = {}


def locked(key):
    n, t = FAILS.get(key, (0, 0))
    return n >= 5 and time.time() - t < 300


def bad(key):
    n, _ = FAILS.get(key, (0, 0))
    FAILS[key] = (n + 1, time.time())


def good(key):
    FAILS.pop(key, None)


# --------------------------------------------------------------------------
# telegram helpers
# --------------------------------------------------------------------------
def tg(method, data=None, files=None, timeout=30):
    tok = S.get("telegram_token")
    if not tok:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/{method}",
                          data=data, files=files, timeout=timeout)
        j = r.json()
        if not j.get("ok"):
            log.warning("telegram %s: %s", method, j.get("description"))
        return j
    except Exception as e:
        log.warning("telegram %s error: %s", method, scrub(e))
        return None


def owners():
    return [str(x) for x in S.get("owner_chat_ids")]


def say(chat, text):
    tg("sendMessage", {"chat_id": chat, "text": text[:4000],
                       "disable_web_page_preview": "true"})


def notify(text):
    for c in owners():
        say(c, text)


def send_clip(path, caption):
    c = str(S.get("clip_chat_id") or "").strip()
    targets = [c] if c else owners()
    if not targets:
        log.warning("koi Telegram chat registered nahi hai (/start <password>)")
        return False
    sent = False
    for chat in targets:
        for _ in range(3):
            with open(path, "rb") as f:
                j = tg("sendVideo", {"chat_id": chat, "caption": caption[:1000],
                                     "supports_streaming": "true"},
                       {"video": f}, timeout=240)
            if j and j.get("ok"):
                sent = True
                break
            time.sleep(3)
    return sent


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
HISTORY = deque(maxlen=50)
AUDIO_USED = {"day": "", "n": 0}


def yt_id(link):
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", link)
    return m.group(1) if m else None


def cookies_file():
    c = (S.get("cookies") or "").strip()
    if not c:
        return None
    with open(COOKIE_FILE, "w") as f:
        f.write(c + "\n")
    os.chmod(COOKIE_FILE, 0o600)
    return COOKIE_FILE


def run_ff(args, timeout=150):
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args,
                           capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception as e:
        log.warning("ffmpeg error: %s", e)
        return False


def fmt_dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def seg_time(path):
    try:
        return int(os.path.basename(path).split(".")[0])
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# watcher: one per live link
# --------------------------------------------------------------------------
WATCHERS = {}
W_LOCK = threading.RLock()


class Watcher:
    def __init__(self, link):
        self.link = link
        self.id = secrets.token_hex(2)
        self.dir = os.path.join(BUF_ROOT, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.stop_ev = threading.Event()
        self.proc = None
        self.state = "starting"
        self.started = time.time()
        self.last_clip = 0
        self.clips = 0
        self.err = ""
        self.video_id = yt_id(link)
        self.chat_state = "-"
        self.clip_lock = threading.Lock()

    # ---- lifecycle
    def start(self):
        for fn in (self.record_loop, self.cleaner_loop, self.chat_loop, self.audio_loop):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self):
        self.stop_ev.set()
        self.state = "stopped"
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

    def finish(self, why):
        """called when the stream is over / unreachable"""
        self.stop()
        drop(self, why)

    def logm(self, msg):
        log.info("[%s] %s", self.id, msg)

    # ---- buffer
    def segments(self):
        return sorted(glob.glob(os.path.join(self.dir, "*.ts")))

    def buffer_len(self):
        return len(self.segments()) * 2

    def cleaner_loop(self):
        while not self.stop_ev.wait(5):
            keep = max(int(S.get("buffer_seconds")), int(S.get("clip_seconds")) + 30)
            segs = self.segments()[:-1]
            for s in segs:
                if time.time() - seg_time(s) > keep:
                    try:
                        os.remove(s)
                    except OSError:
                        pass
        shutil_rm(self.dir)

    def resolve(self):
        h = int(S.get("max_height"))
        cmd = ["yt-dlp", "-g", "--no-warnings", "--no-playlist",
               "-f", f"best[height<={h}]/best"]
        cf = cookies_file()
        if cf:
            cmd += ["--cookies", cf]
        cmd += shlex.split(S.get("ytdlp_extra_args") or "")
        cmd += ["--", self.link]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
            raise RuntimeError(scrub(lines[-1] if lines else "yt-dlp fail"))
        urls = [u for u in r.stdout.split() if u.startswith("http")]
        if not urls:
            raise RuntimeError("stream URL nahi mila")
        return urls[:2]

    def record_loop(self):
        fails = 0
        warned = False
        while not self.stop_ev.is_set():
            try:
                self.state = "resolving"
                urls = self.resolve()
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
                for u in urls:
                    cmd += ["-i", u]
                if len(urls) == 2:
                    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
                else:
                    cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
                cmd += ["-c", "copy", "-f", "segment", "-segment_time", "2",
                        "-strftime", "1", "-reset_timestamps", "1",
                        os.path.join(self.dir, "%s.ts")]
                t0 = time.time()
                with open(os.path.join(self.dir, "ffmpeg.log"), "w") as lf:
                    self.proc = subprocess.Popen(cmd, stderr=lf, stdout=subprocess.DEVNULL)
                    self.state = "recording"
                    self.err = ""
                    self.logm("recording shuru")
                    while self.proc.poll() is None and not self.stop_ev.is_set():
                        time.sleep(1)
                if self.stop_ev.is_set():
                    return
                fails = 0 if time.time() - t0 > 90 else fails + 1
                self.err = "ffmpeg ruk gaya, restart ho raha hai"
            except Exception as e:
                fails += 1
                self.err = scrub(e)[:300]
                self.logm(f"error: {self.err}")
                if "confirm you" in self.err.lower() and not warned:
                    warned = True
                    notify("⚠️ YouTube ne bot-check manga. Admin panel > Settings me "
                           "YouTube cookies (cookies.txt) daalo.")
            if fails >= 6:
                self.finish("Stream khatam ya access nahi ho raha: " + (self.err or "?"))
                return
            self.state = "retrying"
            self.stop_ev.wait(min(5 * 2 ** fails, 60))

    # ---- triggers
    def auto_clip(self, seconds, title, reason, extra=""):
        """non-blocking clip with cooldown (used by chat + audio triggers)"""
        if time.time() - self.last_clip < int(S.get("cooldown_seconds")):
            return False
        self.last_clip = time.time()
        threading.Thread(target=self.make_clip,
                         args=(seconds, title, reason, extra), daemon=True).start()
        return True

    def make_clip(self, seconds, title, reason, extra=""):
        seconds = max(5, min(int(seconds), 120))
        with self.clip_lock:
            segs = self.segments()
            if len(segs) < 2:
                return False, "Buffer abhi khali hai, thoda ruko"
            segs = segs[:-1]  # newest file is still being written
            cutoff = time.time() - seconds - 3
            pick = [s for s in segs if seg_time(s) >= cutoff] or segs[-4:]
            base = os.path.join(OUT_DIR, f"{self.id}_{int(time.time() * 1000)}")
            lst, out = base + ".txt", base + ".mp4"
            ok, msg = False, ""
            try:
                with open(lst, "w") as f:
                    for s in pick:
                        f.write("file '%s'\n" % s.replace("'", "'\\''"))
                good_ = run_ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                                "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out])
                if not good_:
                    good_ = run_ff(["-f", "concat", "-safe", "0", "-i", lst,
                                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                                    "-c:a", "aac", "-movflags", "+faststart", out])
                if not good_ or not os.path.exists(out):
                    msg = "ffmpeg clip nahi bana paaya"
                elif os.path.getsize(out) > 49 * 1024 * 1024:
                    msg = "Clip 50MB se badi hai (Telegram limit). max_height kam karo."
                else:
                    cap = f"🎬 {title}\n{reason} • {datetime.now():%H:%M:%S}"
                    if extra:
                        cap += f"\n{extra}"
                    ok = send_clip(out, cap)
                    msg = "bhej di" if ok else "Telegram par send fail"
            except Exception as e:
                msg = scrub(e)
            finally:
                for p in (lst, out):  # always delete
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            if ok:
                self.clips += 1
            HISTORY.appendleft({"t": f"{datetime.now():%H:%M:%S}", "title": title,
                                "src": reason, "ok": ok, "w": self.id, "msg": msg})
            self.logm(f"clip '{title}' ({reason}): {msg}")
            if not ok:
                notify(f"❌ Clip fail ({title}): {msg}")
            return ok, msg

    # ---- chat trigger (no YouTube API)
    def chat_loop(self):
        try:
            import pytchat
        except ImportError:
            self.chat_state = "pytchat missing"
            return
        while not self.stop_ev.is_set():
            if not S.get("chat_enabled") or "youtu" not in self.link:
                self.chat_state = "off"
                self.stop_ev.wait(10)
                continue
            if not self.video_id:
                self.video_id = self.lookup_id()
                if not self.video_id:
                    self.chat_state = "id nahi mili"
                    self.stop_ev.wait(20)
                    continue
            try:
                chat = pytchat.create(video_id=self.video_id, interruptable=False)
                self.chat_state = "reading"
                while chat.is_alive() and not self.stop_ev.is_set():
                    for c in chat.get().sync_items():
                        try:
                            self.on_chat(c)
                        except Exception as e:
                            log.warning("chat parse: %s", e)
                    time.sleep(1)
                self.chat_state = "reconnecting"
            except Exception as e:
                self.chat_state = "error"
                self.logm(f"chat error: {scrub(e)[:200]}")
            self.stop_ev.wait(10)

    def lookup_id(self):
        try:
            cmd = ["yt-dlp", "--print", "id", "--skip-download", "--no-playlist"]
            cf = cookies_file()
            if cf:
                cmd += ["--cookies", cf]
            r = subprocess.run(cmd + ["--", self.link], capture_output=True, text=True, timeout=60)
            v = r.stdout.strip().splitlines()
            return v[-1] if v and re.fullmatch(r"[A-Za-z0-9_-]{11}", v[-1]) else None
        except Exception:
            return None

    def on_chat(self, c):
        name = (S.get("bot_name") or "").strip()
        if not name:
            return
        m = re.search(r"@?" + re.escape(name) + r"\s+clip\s*cut\b(.*)", c.message or "", re.I | re.S)
        if not m:
            return
        if S.get("chat_who") == "mods_members":
            a = c.author
            if not (a.isChatModerator or a.isChatOwner or a.isChatSponsor):
                return
        rest = m.group(1).strip()
        p = re.search(r"\((.*?)\)", rest)
        title = (p.group(1) if p else rest).strip(" -:") or "Clip"
        title = title[:200]
        self.logm(f"chat command from {c.author.name}: {title}")
        self.auto_clip(int(S.get("clip_seconds")), title, "chat", f"by {c.author.name}")

    # ---- audio trigger (Groq Whisper)
    def audio_loop(self):
        last = time.time()
        while not self.stop_ev.is_set():
            chunk = max(8, int(S.get("audio_chunk_seconds")))
            if self.stop_ev.wait(1) or time.time() - last < chunk:
                continue
            t_start = time.time() - chunk - 2
            last = time.time()
            key = S.get("groq_api_key")
            if not (S.get("audio_enabled") and key and self.state == "recording"):
                continue
            day = datetime.now().strftime("%Y-%m-%d")
            if AUDIO_USED["day"] != day:
                AUDIO_USED.update(day=day, n=0)
            if AUDIO_USED["n"] >= int(S.get("audio_daily_limit")):
                continue
            try:
                self.audio_check(key, t_start)
            except Exception as e:
                self.logm(f"audio error: {scrub(e)[:200]}")

    def audio_check(self, key, t_start):
        segs = self.segments()[:-1]
        pick = [s for s in segs if seg_time(s) >= t_start]
        if len(pick) < 2:
            return
        base = os.path.join(OUT_DIR, f"a_{self.id}_{int(time.time() * 1000)}")
        lst, mp3 = base + ".txt", base + ".mp3"
        try:
            with open(lst, "w") as f:
                for s in pick:
                    f.write("file '%s'\n" % s.replace("'", "'\\''"))
            if not run_ff(["-f", "concat", "-safe", "0", "-i", lst, "-vn", "-ac", "1",
                           "-ar", "16000", "-b:a", "32k", mp3]):
                return
            data = {"model": "whisper-large-v3-turbo", "response_format": "json",
                    "temperature": "0"}
            if S.get("audio_language"):
                data["language"] = S.get("audio_language")
            AUDIO_USED["n"] += 1
            with open(mp3, "rb") as f:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {key}"},
                                  files={"file": ("a.mp3", f, "audio/mpeg")},
                                  data=data, timeout=60)
            if r.status_code == 429:
                self.logm("Groq rate limit (429), 60s pause")
                self.stop_ev.wait(60)
                return
            if r.status_code != 200:
                self.logm(f"Groq {r.status_code}: {scrub(r.text)[:150]}")
                return
            text = (r.json().get("text") or "").strip()
        finally:
            for p in (lst, mp3):
                try:
                    os.remove(p)
                except OSError:
                    pass
        low = text.lower()
        for kw in [k.strip().lower() for k in S.get("keywords").split(",") if k.strip()]:
            if re.search(r"(?<!\w)" + re.escape(kw), low):
                lag = time.time() - t_start
                secs = max(int(S.get("clip_seconds")), int(lag) + 2)
                self.logm(f"keyword '{kw}' mila: {text[:80]}")
                self.auto_clip(secs, f"Auto: {kw}", "audio", f"“{text[:120]}”")
                return


def shutil_rm(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def drop(w, why=None):
    with W_LOCK:
        WATCHERS.pop(w.id, None)
        S.update(active_links=[l for l in S.get("active_links") if l != w.link])
    if why:
        notify(f"⏹ {w.link}\n{why}")


def start_watch(link, persist=True):
    link = link.strip()
    if not re.match(r"https?://\S+$", link):
        return False, "Sahi http/https link do"
    with W_LOCK:
        for w in WATCHERS.values():
            if w.link == link:
                return False, "Ye link pehle se chal raha hai"
        if len(WATCHERS) >= int(S.get("max_streams")):
            return False, f"Max {S.get('max_streams')} streams ek saath (Settings me badha sakte ho)"
        w = Watcher(link)
        WATCHERS[w.id] = w
        w.start()
        if persist and link not in S.get("active_links"):
            S.update(active_links=S.get("active_links") + [link])
    return True, f"Watch shuru ({w.id})"


def stop_watch(ident=None):
    with W_LOCK:
        ws = list(WATCHERS.values())
    n = 0
    for w in ws:
        if ident in (None, "", "all", w.id):
            w.stop()
            drop(w)
            n += 1
    return n


def status_rows():
    rows = []
    with W_LOCK:
        ws = list(WATCHERS.values())
    for w in ws:
        rows.append({"id": w.id, "link": w.link, "state": w.state,
                     "uptime": fmt_dur(time.time() - w.started), "buf": w.buffer_len(),
                     "clips": w.clips, "chat": w.chat_state, "err": w.err})
    return rows


def clip_now(ident, title):
    with W_LOCK:
        ws = list(WATCHERS.values())
    ws = [w for w in ws if ident in (None, "", w.id)]
    if not ws:
        return False, "Koi active stream nahi"
    w = ws[0]
    title = (title or "").strip()[:200] or "Manual clip"
    threading.Thread(target=w.make_clip,
                     args=(int(S.get("clip_seconds")), title, "manual"), daemon=True).start()
    return True, f"Clip ban rahi hai: {title}"


# --------------------------------------------------------------------------
# telegram bot (long polling) - private: only registered owners
# --------------------------------------------------------------------------
HELP = (
    "🎬 ClipBot commands\n"
    "• Live link seedha bhej do (ya /watch <link>)\n"
    "• /stop [id|all] – stream band\n"
    "• /status – kya chal raha hai\n"
    "• /clip [title] – abhi ki last clip\n"
    "• /set <key> <value> – bot_name, clip_seconds, cooldown_seconds, keywords, "
    "audio_enabled, chat_enabled, chat_who\n"
    "• /audio on|off\n"
    "API keys sirf web admin panel se badalte hain."
)
SET_KEYS = {"bot_name": str, "clip_seconds": int, "cooldown_seconds": int, "keywords": str,
            "audio_enabled": bool, "chat_enabled": bool, "chat_who": str}
RANGES = {"clip_seconds": (5, 60), "cooldown_seconds": (0, 600)}


def to_bool(v):
    return v.strip().lower() in ("1", "on", "true", "yes", "haan")


def handle(u):
    m = u.get("message") or {}
    text = (m.get("text") or "").strip()
    chat = str((m.get("chat") or {}).get("id", ""))
    if not text or not chat:
        return
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()

    if cmd == "/start" and chat not in owners():
        if locked("tg" + chat):
            return
        if arg and check_pw(arg):
            good("tg" + chat)
            S.update(owner_chat_ids=owners() + [chat])
            tg("deleteMessage", {"chat_id": chat, "message_id": m.get("message_id")})
            say(chat, "✅ Registered. Ab ye bot sirf tumhara hai.\n\n" + HELP)
        else:
            bad("tg" + chat)
        return
    if chat not in owners():
        return  # private: ignore everyone else silently

    if cmd in ("/start", "/help"):
        say(chat, HELP)
    elif cmd == "/watch" or re.match(r"https?://", text):
        link = arg if cmd == "/watch" else text.split()[0]
        say(chat, ("✅ " if (r := start_watch(link))[0] else "⚠️ ") + r[1])
    elif cmd == "/stop":
        say(chat, f"⏹ {stop_watch(arg or None)} stream band")
    elif cmd == "/status":
        rows = status_rows()
        if not rows:
            say(chat, "Koi stream nahi chal rahi.")
        else:
            say(chat, "\n\n".join(
                f"[{r['id']}] {r['state']} • {r['uptime']} • buffer {r['buf']}s • "
                f"clips {r['clips']} • chat {r['chat']}\n{r['link']}"
                + (f"\n⚠ {r['err']}" if r["err"] else "") for r in rows))
    elif cmd == "/clip":
        ok, msg = clip_now(None, arg)
        say(chat, ("✂️ " if ok else "⚠️ ") + msg)
    elif cmd == "/audio":
        S.update(audio_enabled=to_bool(arg))
        say(chat, f"Audio trigger: {'ON' if S.get('audio_enabled') else 'OFF'}")
    elif cmd == "/set":
        k, _, v = arg.partition(" ")
        if k not in SET_KEYS or not v.strip():
            say(chat, "Use: /set <key> <value>\nKeys: " + ", ".join(SET_KEYS))
            return
        try:
            val = to_bool(v) if SET_KEYS[k] is bool else SET_KEYS[k](v.strip())
            if k in RANGES:
                lo, hi = RANGES[k]
                val = max(lo, min(hi, val))
            if k == "chat_who" and val not in ("all", "mods_members"):
                raise ValueError("all ya mods_members")
        except ValueError as e:
            say(chat, f"Galat value: {e}")
            return
        S.update(**{k: val})
        say(chat, f"✅ {k} = {val}")
    else:
        say(chat, "Samjha nahi. /help dekho.")


def tg_loop():
    offset, last_tok = None, None
    while True:
        tok = S.get("telegram_token")
        if not tok:
            time.sleep(5)
            continue
        try:
            if tok != last_tok:
                requests.post(f"https://api.telegram.org/bot{tok}/deleteWebhook", timeout=20)
                last_tok = tok
            r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates",
                             params={"timeout": 25, "offset": offset}, timeout=40)
            j = r.json()
            if not j.get("ok"):
                log.warning("getUpdates: %s", j.get("description"))
                time.sleep(8)
                continue
            for u in j["result"]:
                offset = u["update_id"] + 1
                try:
                    handle(u)
                except Exception:
                    log.exception("handle error")
        except Exception as e:
            log.warning("telegram poll: %s", scrub(e))
            time.sleep(6)


def keepalive_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        time.sleep(600)
        if S.get("keepalive"):
            try:
                requests.get(url.rstrip("/") + "/health", timeout=20)
            except Exception:
                pass


def resume_loop():
    time.sleep(3)
    for link in list(S.get("active_links")):
        ok, msg = start_watch(link, persist=False)
        log.info("resume %s: %s", link, msg)


_started = False


def start_background():
    global _started
    if _started:
        return
    _started = True
    for fn in (tg_loop, keepalive_loop, resume_loop):
        threading.Thread(target=fn, daemon=True).start()


# --------------------------------------------------------------------------
# web admin panel (password protected)
# --------------------------------------------------------------------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = S.get("secret_key")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

# (key, label, kind, help)
FIELDS = [
    ("🔑 API keys (private)", [
        ("telegram_token", "Telegram bot token", "secret", "@BotFather se. Badalte hi bot naye token par chalne lagta hai."),
        ("groq_api_key", "Groq API key (Whisper)", "secret", "Audio keyword trigger ke liye (free)."),
        ("youtube_api_key", "YouTube API key (optional)", "secret", "Sirf auth/future use. Chat/clip iske bina chalta hai."),
        ("cookies", "YouTube cookies.txt (Netscape format)", "secret_area", "Render par 'Sign in to confirm you're not a bot' aaye to yahan paste karo. Alag/throwaway account use karo."),
        ("clip_chat_id", "Clips ka chat/channel ID (optional)", "text", "Khali = tumhare registered chat me. Channel ho to bot ko admin banao (-100... ya @name)."),
    ]),
    ("✂️ Clip", [
        ("bot_name", "Bot name (chat command)", "text", "Chat me likho: <name> clip cut (title)"),
        ("clip_seconds", "Clip length (sec)", "number", "5-60"),
        ("cooldown_seconds", "Cooldown (sec)", "number", "Do auto clips ke beech minimum gap"),
        ("buffer_seconds", "Buffer (sec)", "number", "40-300, RAM/disk /tmp me"),
        ("max_height", "Max video height", "number", "480/720 - chhota = chhoti file"),
    ]),
    ("💬 Chat trigger", [
        ("chat_enabled", "Chat command ON", "bool", ""),
        ("chat_who", "Kaun command de sakta hai", "select:all,mods_members", "mods_members = sirf mod/owner/member"),
    ]),
    ("🎧 Audio trigger", [
        ("audio_enabled", "Audio keyword trigger ON", "bool", ""),
        ("keywords", "Keywords (comma separated)", "text", "Hindi/English dono"),
        ("audio_chunk_seconds", "Har kitne sec ka audio check ho", "number", "8-60"),
        ("audio_daily_limit", "Roz ke max Groq requests", "number", "Free limit se neeche rakho"),
        ("audio_language", "Language code (optional)", "text", "hi / en. Khali = auto"),
    ]),
    ("⚙️ System", [
        ("max_streams", "Max parallel streams", "number", "1-5"),
        ("keepalive", "Self keep-alive ping", "bool", "Free plan par sleep rokne ki koshish"),
        ("ytdlp_extra_args", "yt-dlp extra args", "text", "Advanced, e.g. --js-runtimes node"),
    ]),
]
NUM_RANGE = {"clip_seconds": (5, 60), "cooldown_seconds": (0, 600), "buffer_seconds": (40, 300),
             "max_height": (144, 1080), "audio_chunk_seconds": (8, 60),
             "audio_daily_limit": (0, 100000), "max_streams": (1, 5)}

BASE = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ClipBot</title>
{% block head %}{% endblock %}
<style>
:root{--bg:#0f1115;--card:#181b22;--b:#262a34;--t:#e8eaf0;--m:#8b93a7;--a:#ff4d4d;--ok:#3ecf8e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:15px/1.5 system-ui,sans-serif}
.w{max-width:860px;margin:0 auto;padding:14px}
nav{display:flex;gap:16px;align-items:center;margin-bottom:14px}nav a{color:var(--m);text-decoration:none}nav b{color:var(--a)}
.c{background:var(--card);border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:14px}
h2{margin:0 0 10px;font-size:16px}
input,select,textarea{width:100%;background:#0f1115;color:var(--t);border:1px solid var(--b);border-radius:8px;padding:9px;font:inherit}
input[type=checkbox]{width:auto}
label{display:block;margin:12px 0 3px;color:var(--m);font-size:13px}
button{background:var(--a);color:#fff;border:0;border-radius:8px;padding:9px 14px;font:inherit;cursor:pointer}
button.g{background:#2a2f3b}
.r{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px}.r input{flex:1;min-width:150px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px;border-bottom:1px solid var(--b);text-align:left;vertical-align:top;word-break:break-word}
.s{display:inline-block;padding:1px 8px;border-radius:99px;background:#2a2f3b;font-size:12px}.s.recording{background:#16382b;color:var(--ok)}
pre{white-space:pre-wrap;font-size:12px;color:var(--m);max-height:260px;overflow:auto;margin:0}
.e{color:#ff8a8a}small{color:var(--m)}.ok{color:var(--ok)}
</style></head><body><div class="w">
{% if session.get('ok') %}<nav><b>● ClipBot</b><a href="/">Dashboard</a><a href="/settings">Settings</a><a href="/logout">Logout</a></nav>{% endif %}
{% for m in get_flashed_messages() %}<div class="c">{{ m }}</div>{% endfor %}
{% block body %}{% endblock %}
</div></body></html>"""

LOGIN = """{% extends "base.html" %}{% block body %}
<div class="c" style="max-width:380px;margin:60px auto"><h2>🔒 ClipBot login</h2>
<form method="post"><label>Admin password</label><input type="password" name="pw" autofocus required>
<div class="r"><button>Login</button></div></form></div>{% endblock %}"""

DASH = """{% extends "base.html" %}
{% block head %}<meta http-equiv="refresh" content="20">{% endblock %}
{% block body %}
<div class="c"><h2>Live link add karo</h2>
<form method="post" action="/watch" class="r"><input type="hidden" name="csrf" value="{{ csrf }}">
<input name="link" placeholder="https://youtube.com/live/..." required><button>▶ Watch shuru</button></form>
<small>Telegram me bhi link bhej sakte ho. Max {{ maxs }} streams. Setup: {{ setup }}</small></div>
<div class="c"><h2>Chal rahe streams ({{ ws|length }})</h2>
{% if not ws %}<small>Koi stream nahi chal rahi.</small>{% endif %}
{% for w in ws %}<div style="margin-bottom:16px">
<div class="r" style="margin:0"><span class="s {{ w.state }}">{{ w.state }}</span><b>{{ w.id }}</b>
<small>{{ w.uptime }} • buffer ~{{ w.buf }}s • clips {{ w.clips }} • chat {{ w.chat }}</small></div>
<div style="word-break:break-all"><small>{{ w.link }}</small></div>
{% if w.err %}<div class="e"><small>{{ w.err }}</small></div>{% endif %}
<form method="post" action="/clip" class="r"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="id" value="{{ w.id }}"><input name="title" placeholder="Clip title (optional)"><button>✂ Clip now</button></form>
<form method="post" action="/stop" class="r"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="id" value="{{ w.id }}"><button class="g">⏹ Stop</button></form></div>{% endfor %}</div>
<div class="c"><h2>Recent clips</h2>
{% if not hist %}<small>Abhi tak koi clip nahi.</small>{% else %}<table>
{% for h in hist %}<tr><td>{{ h.t }}</td><td>{{ h.title }}</td><td>{{ h.src }}</td>
<td class="{{ 'ok' if h.ok else 'e' }}">{{ h.msg }}</td></tr>{% endfor %}</table>{% endif %}</div>
<div class="c"><h2>Log</h2><pre>{{ logs }}</pre></div>{% endblock %}"""

SETT = """{% extends "base.html" %}{% block body %}
<form method="post" action="/settings"><input type="hidden" name="csrf" value="{{ csrf }}">
{% for title, fs in sections %}<div class="c"><h2>{{ title }}</h2>
{% for f in fs %}
{% if f.kind == 'bool' %}<label><input type="checkbox" name="{{ f.key }}" {{ 'checked' if f.value }}> {{ f.label }}</label>
{% elif f.kind == 'secret' or f.kind == 'secret_area' %}<label>{{ f.label }} {% if f.set %}<span class="ok">✓ saved</span>{% endif %}</label>
{% if f.kind == 'secret_area' %}<textarea name="{{ f.key }}" rows="3" placeholder="{{ 'saved - naya paste karo to replace hoga' if f.set else '' }}"></textarea>
{% else %}<input type="password" name="{{ f.key }}" autocomplete="off" placeholder="{{ 'saved - naya daalo to replace hoga' if f.set else '' }}">{% endif %}
{% if f.set %}<label><input type="checkbox" name="clear_{{ f.key }}"> hata do</label>{% endif %}
{% elif f.kind.startswith('select') %}<label>{{ f.label }}</label><select name="{{ f.key }}">
{% for o in f.kind.split(':')[1].split(',') %}<option {{ 'selected' if o == f.value }}>{{ o }}</option>{% endfor %}</select>
{% else %}<label>{{ f.label }}</label><input name="{{ f.key }}" value="{{ f.value }}" {{ 'type=number' if f.kind == 'number' }}>{% endif %}
{% if f.help %}<small>{{ f.help }}</small>{% endif %}{% endfor %}</div>{% endfor %}
<div class="r"><button>💾 Save settings</button></div></form>
<div class="c" style="margin-top:14px"><h2>Tools</h2><div class="r">
<form method="post" action="/test/telegram"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="g">Telegram test</button></form>
<form method="post" action="/test/groq"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="g">Groq test</button></form>
<form method="post" action="/ytdlp/update"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="g">yt-dlp update</button></form></div></div>
<div class="c"><h2>Admin password badlo</h2>
<form method="post" action="/password"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Naya password (min 10 char)</label><input type="password" name="pw" minlength="10" required>
<div class="r"><button class="g">Change</button></div></form></div>{% endblock %}"""

app.jinja_env.loader = DictLoader({"base.html": BASE, "login.html": LOGIN,
                                   "dash.html": DASH, "settings.html": SETT})


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"]}


@app.before_request
def guard():
    p = request.path
    if p in ("/health", "/login"):
        return
    if not session.get("ok"):
        return redirect("/login")
    if request.method == "POST" and not hmac.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "x")):
        return "Bad CSRF token", 400


@app.after_request
def headers(r):
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/health")
def health():
    return jsonify(ok=True, streams=len(WATCHERS))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = "ip" + (request.remote_addr or "?")
        if locked(ip):
            flash("Bahut galat koshish. 5 min baad try karo.")
        elif check_pw(request.form.get("pw", "")):
            good(ip)
            session.clear()
            session["ok"] = True
            session.permanent = True
            return redirect("/")
        else:
            bad(ip)
            flash("Galat password")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def dash():
    setup = []
    if not S.get("telegram_token"):
        setup.append("⚠ Telegram token daalo")
    if not owners():
        setup.append("⚠ Telegram me /start <password> bhejo")
    if not S.get("groq_api_key"):
        setup.append("ℹ Groq key nahi (audio trigger off)")
    return render_template("dash.html", ws=status_rows(), hist=list(HISTORY)[:15],
                           logs="\n".join(list(LOGS)[-60:][::-1]), maxs=S.get("max_streams"),
                           setup=" • ".join(setup) or "✓ ready")


@app.route("/watch", methods=["POST"])
def w_watch():
    flash(start_watch(request.form.get("link", ""))[1])
    return redirect("/")


@app.route("/stop", methods=["POST"])
def w_stop():
    flash(f"{stop_watch(request.form.get('id'))} stream band")
    return redirect("/")


@app.route("/clip", methods=["POST"])
def w_clip():
    flash(clip_now(request.form.get("id"), request.form.get("title"))[1])
    return redirect("/")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        upd = {}
        for _, fs in FIELDS:
            for key, _l, kind, _h in fs:
                v = request.form.get(key, "")
                if kind in ("secret", "secret_area"):
                    if request.form.get("clear_" + key):
                        upd[key] = ""
                    elif v.strip():
                        upd[key] = v.strip()
                elif kind == "bool":
                    upd[key] = key in request.form
                elif kind == "number":
                    try:
                        lo, hi = NUM_RANGE[key]
                        upd[key] = max(lo, min(hi, int(v)))
                    except (ValueError, KeyError):
                        pass
                elif kind.startswith("select"):
                    if v in kind.split(":")[1].split(","):
                        upd[key] = v
                else:
                    upd[key] = v.strip()
        S.update(**upd)
        flash("Saved ✓ (token badla ho to bot ~30 sec me naye token par aa jayega)")
        return redirect("/settings")
    sections = []
    for title, fs in FIELDS:
        items = []
        for key, label, kind, help_ in fs:
            val = S.get(key)
            items.append({"key": key, "label": label, "kind": kind, "help": help_,
                          "value": "" if kind.startswith("secret") else val,
                          "set": bool(val) if kind.startswith("secret") else False})
        sections.append((title, items))
    return render_template("settings.html", sections=sections)


@app.route("/test/telegram", methods=["POST"])
def t_tg():
    if not S.get("telegram_token"):
        flash("Pehle Telegram token save karo")
    elif not owners():
        flash("Telegram me bot ko /start <admin password> bhejo")
    else:
        notify("✅ Test message - ClipBot chal raha hai")
        flash("Test message bhej diya")
    return redirect("/settings")


@app.route("/test/groq", methods=["POST"])
def t_groq():
    key = S.get("groq_api_key")
    if not key:
        flash("Groq key save nahi hai")
    else:
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=20)
            flash("Groq key OK ✓" if r.status_code == 200 else f"Groq error {r.status_code}")
        except Exception as e:
            flash("Groq error: " + scrub(e)[:120])
    return redirect("/settings")


def _upd_ytdlp():
    r = subprocess.run(["python", "-m", "pip", "install", "-U", "--quiet", "yt-dlp[default]"],
                       capture_output=True, text=True)
    log.info("yt-dlp update rc=%s %s", r.returncode, r.stderr.strip()[-150:])


@app.route("/ytdlp/update", methods=["POST"])
def t_ytdlp():
    threading.Thread(target=_upd_ytdlp, daemon=True).start()
    flash("yt-dlp update shuru (Log me result dikhega). Chalte streams restart hone par naya use karenge.")
    return redirect("/settings")


@app.route("/password", methods=["POST"])
def chpw():
    pw = request.form.get("pw", "")
    if len(pw) < 10:
        flash("Password kam se kam 10 character")
    else:
        S.update(admin_password_hash=generate_password_hash(pw))
        flash("Password badal gaya ✓")
    return redirect("/settings")


start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
