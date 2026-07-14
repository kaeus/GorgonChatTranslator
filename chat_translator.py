#!/usr/bin/env python3
"""
Project Gorgon — Live Chat Translator Overlay
=============================================
A draggable, resizable, always-on-top dark overlay that tails the newest
Project Gorgon chat log and shows translations of FOREIGN-language messages
from the channels you care about (default: Global, Local, Party, Trade) into
YOUR language. Messages already in your language are ignored -- you can read
those in-game, and skipping them is what keeps translation-API usage tiny.

Two-way chat
------------
It also remembers the last foreign language it saw someone use. Press the
reverse hotkey (default Ctrl+Alt+T) or click the "⇄" button and it translates
YOUR most recent outgoing message (any line spoken by `player_name` in a
watched channel) FROM your language INTO that foreign language and copies it to
the clipboard -- paste it straight into game chat to reply.

Single file. Requires:
    pip install deep-translator
Optional but STRONGLY recommended -- needed for accurate detection AND for the
reverse feature to know which language to reply in:
    pip install langdetect

Config lives in chat_translator_config.json next to this script.".
"""

import json
import os
import re
import sys
import time
import queue
import glob
import threading
import tkinter as tk
from tkinter import font as tkfont

# Shared helper in c:\projects: builds a unique identicon .ico and sets a
# per-app Windows AppUserModelID (own taskbar group/icon, like the other apps).
try:
    sys.path.insert(0, r"c:\projects")
    import appicon
except Exception:
    appicon = None

APP_NAME = "Gorgon Chat Translator"
APP_ID   = "Kaeus.GorgonChatTranslator"

# --------------------------------------------------------------------------- #
#  Paths / constants
# --------------------------------------------------------------------------- #
APP_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "chat_translator_config.json")


def _res_path(name):
    """Path to a bundled resource -- works from source and from the frozen exe
    (PyInstaller unpacks datas into sys._MEIPASS)."""
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), name)


def _read_version():
    try:
        with open(_res_path("version.txt"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except Exception:
        return "dev"


def _version_tuple(v):
    v = v.lstrip("vV").split("-")[0]
    out = []
    for p in v.split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


APP_VERSION = _read_version()

PG_DIR   = os.path.normpath(os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "..", "LocalLow",
    "Elder Game", "Project Gorgon"))
LOG_DIR  = os.path.join(PG_DIR, "ChatLogs")

PG_WINDOW_TITLE = "Project Gorgon"    # foreground match for show/hide

DEFAULT_CONFIG = {
    "player_name": "Kaeus",     # your character -- used to find YOUR messages
    "my_lang": "en",            # your language: foreign msgs translate INTO it,
                                # and your replies translate FROM it
    # Language your replies are translated INTO. null = auto (use the last
    # language seen on the channel you last spoke in). Set a code (e.g. "es")
    # from the dropdown to force it -- handy to preset before anyone speaks.
    "reply_lang": None,
    # Watch every channel EXCEPT these. Add channels you want to ignore.
    "blacklist_channels": ["Combat", "Status", "NPC Chatter", "Error"],
    "reverse_hotkey": "ctrl+alt+t",
    "min_api_interval": 0.40,   # seconds between translation API calls
    "min_len": 4,               # ignore super-short msgs ("ty") -> unreliable
    "show_english": False,      # True also shows your-language msgs (no API)
    "show_own": False,          # True echoes your own outgoing msgs, dimmed
    "max_lines": 400,           # trim overlay history to this many lines
    "opacity": 0.92,
    "font_size": 10,
    "hide_when_pg_inactive": False,
    "locked": False,
    "geometry": None,
    # Optional in-app update check: set "owner/repo" to be told (in the overlay)
    # when a newer release exists. Empty = disabled.
    "github_repo": "",
    "check_updates": True,
}

# --------------------------------------------------------------------------- #
#  Dependencies
# --------------------------------------------------------------------------- #
try:
    from deep_translator import GoogleTranslator
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install deep-translator")

try:
    from langdetect import detect as _lang_detect, DetectorFactory
    DetectorFactory.seed = 0
    HAVE_LANGDETECT = True
except ImportError:
    HAVE_LANGDETECT = False

# --------------------------------------------------------------------------- #
#  Shared state between threads
# --------------------------------------------------------------------------- #
class Shared:
    """Thread-safe scratch shared by watcher / translator / UI.

    The foreign language is tracked PER CHANNEL so a reply is scoped to the
    channel you last spoke in -- a different language used in Global won't
    hijack the reply you're composing in Party.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.last_self_msg = None       # your most recent outgoing text
        self.last_self_channel = None   # the channel you last spoke in
        self.foreign_by_channel = {}    # channel -> (lang, speaker)
        self.seen_langs = {}            # code -> {count, speaker, channel}
        self.reply_override = None      # forced reply language, or None = auto

    def set_reply_override(self, code):
        with self.lock:
            self.reply_override = code or None

    def set_self(self, channel, txt):
        with self.lock:
            self.last_self_msg = txt
            self.last_self_channel = channel

    def set_foreign(self, channel, lang, speaker):
        with self.lock:
            self.foreign_by_channel[channel] = (lang, speaker)

    def note_seen(self, lang, speaker, channel):
        with self.lock:
            e = self.seen_langs.get(lang)
            if e:
                e["count"] += 1
                e["speaker"], e["channel"] = speaker, channel
            else:
                self.seen_langs[lang] = {"count": 1, "speaker": speaker,
                                         "channel": channel}

    def seen_list(self):
        """[(code, count, last_speaker, last_channel), ...] most-seen first."""
        with self.lock:
            items = sorted(self.seen_langs.items(),
                           key=lambda kv: -kv[1]["count"])
            return [(c, v["count"], v["speaker"], v["channel"])
                    for c, v in items]

    def reply_target(self):
        """(text, lang, speaker, channel) for a reply.

        If a manual reply language is set it always wins (speaker=None, so the
        UI knows it's a forced choice). Otherwise it's auto: the last foreign
        language seen on the channel of your last outgoing message."""
        with self.lock:
            txt, ch = self.last_self_msg, self.last_self_channel
            if self.reply_override:
                return txt, self.reply_override, None, ch
            lang, sp = self.foreign_by_channel.get(ch, (None, None))
            return txt, lang, sp, ch

# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Created default config at {CONFIG_PATH}")
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.pop("channels", None)          # migrate: old whitelist -> blacklist
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg

# --------------------------------------------------------------------------- #
#  Parsing / language logic
# --------------------------------------------------------------------------- #
# Line format:  "26-07-13 16:03:49\t[Global] Cyrce: text..."
LINE_RE = re.compile(r"^\[(?P<ch>[^\]]+)\]\s+(?P<sp>[^:]+):\s*(?P<txt>.*)$")


def parse_line(raw, blacklist, me):
    """Return (channel, speaker, text, scope, is_self) or None.

    Handles the directed 'A->B' speaker used by Tells: outgoing
    '[Tell] You->Bob' is your own message; incoming '[Tell] Bob->You' is from
    Bob. For those the scope key is per-conversation ('Tell:Bob') so a reply is
    matched to that person's language, not to Tells in general. For normal
    channels the scope is just the channel name.
    """
    if "\t" not in raw:
        return None
    _ts, rest = raw.split("\t", 1)
    m = LINE_RE.match(rest)
    if not m:
        return None
    ch = m.group("ch").strip()
    if ch in blacklist:
        return None
    sp = m.group("sp").strip()
    txt = m.group("txt").strip()
    scope, is_self = ch, False
    if "->" in sp:
        a, b = (p.strip() for p in sp.split("->", 1))
        if a.lower() == "you":              # outgoing tell -> your message
            is_self, sp, scope = True, f"You→{b}", f"{ch}:{b}"
        elif b.lower() == "you":            # incoming tell -> from A
            sp, scope = a, f"{ch}:{a}"
        else:
            scope = f"{ch}:{sp}"
    elif me and sp.lower() == me:
        is_self = True
    return ch, sp, txt, scope, is_self


def _has_non_latin(text):
    # Cyrillic / CJK / Hangul / Arabic / etc. live above the Latin blocks.
    return any(ord(c) > 0x2AF for c in text)


# English function words + Project Gorgon trade/combat jargon. If a Latin-script
# line contains any of these it is treated as English and never translated.
# langdetect is wildly unreliable on short English chat ("LFM Goblin Dungeon"
# scores German at 0.9999), so this guard is what stops English lines from
# wasting API calls AND from corrupting the reverse-translate target language.
# Deliberately excludes tokens shared with Romance languages (de, la, el, un,
# no, va, al, por, para, con, en, le, les, des, pour...) so real Spanish/French
# lines still fall through to translation.
EN_HINTS = frozenset("""
the and you your are for that this with have was not but all get got out now
how who what why when where which would could should will cant dont doesnt
isnt wont anyone someone anybody everyone nobody need want going gonna wanna
come coming here there they them from about please thanks thank help looking
sell selling buy buying selling trade trading price each does did say again
still just know been being only over under into back down more then than too
also much many very off make made take gimme give were your yours mine ours
lfm lfg lfp wts wtb wtt wts pst gz grats gg ez afk brb thx np gl hf dps aoe
buff debuff nerf proc respec alt casino daily anyone gonna
""".split())


def _looks_english(text):
    return any(tok in EN_HINTS for tok in re.findall(r"[a-z]+", text.lower()))


def guess_source_by_script(text):
    """Map the dominant Unicode script to a translation source language.

    Unicode ranges identify the writing system unambiguously (unlike langdetect,
    which mislabels short CJK -- e.g. Chinese as Korean). Used both to pick a
    reliable reply-target language and to retry when GoogleTranslator's
    source='auto' silently fails to translate short CJK text. Returns a
    deep-translator source code, or None for Latin / unknown scripts.
    """
    has = lambda lo, hi: any(lo <= ord(c) <= hi for c in text)
    if has(0x3040, 0x30FF):                       # Hiragana / Katakana
        return "ja"
    if has(0xAC00, 0xD7A3) or has(0x1100, 0x11FF):  # Hangul
        return "ko"
    if has(0x0400, 0x04FF):                       # Cyrillic
        return "ru"
    if has(0x0600, 0x06FF):                       # Arabic
        return "ar"
    if has(0x0E00, 0x0E7F):                       # Thai
        return "th"
    if has(0x4E00, 0x9FFF) or has(0x3400, 0x4DBF):  # Han (Chinese)
        return "zh-CN"
    return None


def detect_lang(text, min_len):
    """Best-effort source-language code, or None if we can't/shouldn't tell.

    Returns a langdetect code ('es', 'ru', 'zh-cn', ...) for foreign text, the
    sentinel '??' for an obviously-foreign non-Latin script we can't name,
    'en'-ish (== my language handling upstream) for text that looks English, or
    None for too-short / undetectable text.
    """
    if len(text) < min_len:
        return None
    # Non-Latin script (Cyrillic/CJK/Arabic/...) is unambiguously foreign.
    if _has_non_latin(text):
        if HAVE_LANGDETECT:
            try:
                return _lang_detect(text)
            except Exception:
                return "??"
        return "??"
    # Latin script: an English function word / jargon token means English.
    if _looks_english(text):
        return "en"
    if not HAVE_LANGDETECT:
        return None          # can't reliably name Latin-script foreign text
    try:
        return _lang_detect(text)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Chat-log tailing  (thread) -> pushes msgs onto in_q
# --------------------------------------------------------------------------- #
def latest_log():
    files = glob.glob(os.path.join(LOG_DIR, "Chat-*.log"))
    return max(files, key=os.path.getmtime) if files else None


class ChatWatcher(threading.Thread):
    """Tails the newest Chat-*.log, surviving the daily file rollover.

    Opened only momentarily each poll (never held open) so we never lock it.
    Tracks a byte offset and dispatches only complete newlines. When a newer
    log appears (new day), switches to it and reads from its start.
    """

    def __init__(self, cfg, shared, in_q):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.q = in_q
        self.blacklist = set(cfg.get("blacklist_channels", []))
        self.me = str(cfg.get("player_name", "")).strip().lower()
        self.stop_evt = threading.Event()

    def _dispatch(self, ch, sp, txt, scope, is_self):
        # Your own outgoing line: record it (scoped) for reverse translation;
        # don't translate it. Optionally echo it into the overlay, dimmed.
        if is_self:
            self.shared.set_self(scope, txt)
            self.q.put(("own", scope, ch, sp, txt))
            return
        self.q.put(("msg", scope, ch, sp, txt))

    def run(self):
        current = None
        pos = None
        while not self.stop_evt.is_set():
            newest = latest_log()
            if newest and newest != current:
                first = current is None      # first attach -> skip old history
                current = newest
                try:
                    pos = os.path.getsize(current) if first else 0
                except OSError:
                    pos = 0
            if current:
                try:
                    size = os.path.getsize(current)
                except OSError:
                    time.sleep(0.5)
                    continue
                if pos is None:
                    pos = size
                elif size < pos:             # truncated -> restart
                    pos = 0
                if size > pos:
                    try:
                        with open(current, "rb") as fh:
                            fh.seek(pos)
                            chunk = fh.read(size - pos)
                    except OSError:
                        time.sleep(0.3)
                        continue
                    nl = chunk.rfind(b"\n")
                    if nl != -1:
                        complete = chunk[:nl + 1]
                        pos += len(complete)
                        for raw in complete.split(b"\n"):
                            if not raw:
                                continue
                            line = raw.decode("utf-8", "replace").rstrip("\r")
                            parsed = parse_line(line, self.blacklist, self.me)
                            if parsed:
                                self._dispatch(*parsed)
            time.sleep(0.3)

    def stop(self):
        self.stop_evt.set()


# --------------------------------------------------------------------------- #
#  Translator worker  (thread) -> pushes display items onto out_q
# --------------------------------------------------------------------------- #
class Translator(threading.Thread):
    def __init__(self, cfg, shared, in_q, out_q):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.in_q = in_q
        self.out_q = out_q
        self.stop_evt = threading.Event()
        self.min_len = int(cfg.get("min_len", 4))
        self.interval = float(cfg.get("min_api_interval", 0.4))
        self.show_english = bool(cfg.get("show_english", False))
        self.show_own = bool(cfg.get("show_own", False))
        self.my_lang = cfg.get("my_lang", "en")
        self.engine = GoogleTranslator(source="auto", target=self.my_lang)
        self.cache = {}
        self.last_call = 0.0
        self.backoff = 1.0
        self._pid = 0          # id for pending/spinner lines

    def _throttle(self):
        wait = self.interval - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)

    def _translate(self, txt):
        """Translate txt into my_lang, returning (result, source_used).

        GoogleTranslator's source='auto' silently no-ops on some short CJK text
        (returns the input unchanged). When that happens we retry with a source
        language guessed from the Unicode script, which is reliable.
        """
        result = self.engine.translate(txt)
        if result and result.strip() != txt.strip():
            return result, "auto"
        src = guess_source_by_script(txt)
        if src and src != self.my_lang:
            try:
                r2 = GoogleTranslator(source=src,
                                      target=self.my_lang).translate(txt)
                if r2 and r2.strip() != txt.strip():
                    return r2, src
            except Exception:
                pass
        return result, None

    def _handle_incoming(self, scope, ch, sp, txt):
        code = detect_lang(txt, self.min_len)
        is_foreign = code is not None and code != self.my_lang
        if not is_foreign:
            if self.show_english:
                self.out_q.put(("_line", ch, sp, txt, None, code))
            return
        # Reply-target language, recorded PER SCOPE (channel, or Tell:person).
        # Script detection is far more reliable than langdetect for CJK (which
        # mislabels Chinese as Korean), so prefer it for non-Latin text.
        reply_lang = guess_source_by_script(txt) or (code if code != "??" else None)
        if reply_lang and reply_lang != self.my_lang:
            self.shared.set_foreign(scope, reply_lang, sp)
            self.shared.note_seen(reply_lang, sp, ch)
        lang = reply_lang or code          # normalized source language for display

        if txt in self.cache:
            self.out_q.put(("_line", ch, sp, self.cache[txt], txt, lang))
            return

        # Show a spinner line right away so you can see the message was caught,
        # then resolve it in place when the translation returns.
        self._pid += 1
        pid = self._pid
        self.out_q.put(("_pending", pid, ch, sp, lang, txt))
        self._throttle()
        try:
            result, _used = self._translate(txt)
            self.last_call = time.time()
            self.backoff = 1.0
            if result and result.strip().lower() != txt.strip().lower():
                self.cache[txt] = result
                self.out_q.put(("_resolve", pid, result, txt))
            elif _has_non_latin(txt):
                # Clearly foreign but Google refused to translate it -- show the
                # original with a note rather than silently dropping it.
                self.out_q.put(("_resolve", pid,
                                txt + "   (couldn't translate)", None))
            elif self.show_english:
                self.out_q.put(("_resolve", pid, txt, None))
            else:
                self.out_q.put(("_resolve", pid, None, None))   # drop the line
        except Exception as e:
            self.out_q.put(("_resolve", pid, txt + "   (retrying…)", None))
            time.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 60)
            self.in_q.put(("msg", scope, ch, sp, txt))   # retry later (with scope)

    def _handle_reverse(self, text, lang):
        self._throttle()
        try:
            rev = GoogleTranslator(source=self.my_lang, target=lang).translate(text)
            self.last_call = time.time()
            self.out_q.put(("_clip", lang, rev, text))
        except Exception as e:
            self.out_q.put(("_sys", f"[reverse error: {e}]"))

    def run(self):
        while not self.stop_evt.is_set():
            try:
                item = self.in_q.get(timeout=0.3)
            except queue.Empty:
                continue
            kind = item[0]
            if kind == "msg":
                self._handle_incoming(item[1], item[2], item[3], item[4])
            elif kind == "own":
                if self.show_own:
                    self.out_q.put(("_own", item[2], item[3], item[4]))
            elif kind == "reverse":
                self._handle_reverse(item[1], item[2])

    def stop(self):
        self.stop_evt.set()


# --------------------------------------------------------------------------- #
#  Global hotkey (Windows RegisterHotKey, stdlib ctypes)
# --------------------------------------------------------------------------- #
def parse_hotkey(s):
    """'ctrl+alt+t' -> (fsModifiers, vk).  Returns (0, None) if unusable."""
    MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4,
            "win": 0x8, "super": 0x8}
    mods, vk = 0, None
    for part in str(s).lower().split("+"):
        part = part.strip()
        if part in MODS:
            mods |= MODS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1        # VK_F1 == 0x70
    return mods, vk


class HotkeyThread(threading.Thread):
    """Registers a system-wide hotkey and fires on_fire() when pressed."""

    WM_HOTKEY = 0x0312

    def __init__(self, hotkey, on_fire):
        super().__init__(daemon=True)
        self.mods, self.vk = parse_hotkey(hotkey)
        self.on_fire = on_fire
        self.stop_evt = threading.Event()
        self.ok = False

    def run(self):
        if os.name != "nt" or not self.vk:
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        # MOD_NOREPEAT (0x4000) so holding the key fires once.
        if not user32.RegisterHotKey(None, 1, self.mods | 0x4000, self.vk):
            print("Could not register reverse hotkey (already in use?).")
            return
        self.ok = True
        msg = wintypes.MSG()
        try:
            while not self.stop_evt.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == self.WM_HOTKEY:
                        try:
                            self.on_fire()
                        except Exception:
                            pass
                time.sleep(0.03)
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        self.stop_evt.set()


# --------------------------------------------------------------------------- #
#  Overlay UI (Tkinter) -- mirrors buff_tracker.py chrome
# --------------------------------------------------------------------------- #
BG       = "#0d0f14"
PANEL    = "#161a22"
HEADER   = "#11141b"
FG       = "#e6e6e6"
MUTED    = "#5a6373"
ACCENT   = "#4aa3ff"
GOOD     = "#39d98a"
WARN     = "#f5a623"
BAD      = "#ff5c5c"
ORIG_FG  = "#7a8091"

CHANNEL_COLORS = {
    "Global": "#7fb4ff",
    "Local":  "#8ce0a0",
    "Party":  "#ffd27f",
    "Trade":  "#e79bff",
    "Help":   "#c0c6d2",
}
DEFAULT_CH_COLOR = "#cfd3dc"

# Common target-language choices for the "→" dropdown (label, deep-translator
# code). Any language seen in chat is also offered under "Seen this session".
COMMON_LANGS = [
    ("English", "en"), ("Spanish", "es"), ("French", "fr"), ("German", "de"),
    ("Portuguese", "pt"), ("Italian", "it"), ("Russian", "ru"),
    ("Ukrainian", "uk"), ("Polish", "pl"), ("Dutch", "nl"),
    ("Chinese (Simpl.)", "zh-CN"), ("Chinese (Trad.)", "zh-TW"),
    ("Japanese", "ja"), ("Korean", "ko"), ("Thai", "th"),
    ("Vietnamese", "vi"), ("Indonesian", "id"), ("Arabic", "ar"),
    ("Turkish", "tr"), ("Hindi", "hi"), ("Swedish", "sv"),
    ("Norwegian", "no"), ("Finnish", "fi"), ("Danish", "da"),
    ("Czech", "cs"), ("Greek", "el"), ("Hungarian", "hu"),
    ("Romanian", "ro"), ("Filipino", "tl"),
]
LANG_NAMES = {code: label for label, code in COMMON_LANGS}


class ChatOverlay(tk.Tk):
    def __init__(self, cfg, shared):
        super().__init__()
        self.cfg = cfg
        self.shared = shared
        self.shared.set_reply_override(cfg.get("reply_lang"))
        self.out_q = queue.Queue()
        self.in_q = queue.Queue()
        self.cmd_q = queue.Queue()
        self.line_count = 0
        self._pending = {}          # pid -> (tag, spin_mark, ch, sp, lang)
        self._spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0

        self._locked = bool(cfg.get("locked", False))
        self._hide_when_inactive = bool(cfg.get("hide_when_pg_inactive", True))
        self._pg_hwnd = None
        self._own_hwnd = None
        self._visible = True

        self._build_window()
        self._build_widgets()

        self.watcher = ChatWatcher(cfg, shared, self.in_q)
        self.translator = Translator(cfg, shared, self.in_q, self.out_q)
        self.hotkey = HotkeyThread(cfg.get("reverse_hotkey", "ctrl+alt+t"),
                                   lambda: self.cmd_q.put("reverse"))
        self.watcher.start()
        self.translator.start()
        self.hotkey.start()

        self.after(10, self._enable_taskbar)
        self.after(120, self._poll)
        self.after(90, self._spin_tick)     # animate pending-translation spinners
        self.after(600, self._focus_tick)   # always runs; honors the live flag
        self.after(1500, self._check_updates)

    # -- window chrome -------------------------------------------------------
    def _build_window(self):
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", float(self.cfg.get("opacity", 0.92)))
        except Exception:
            pass
        self.configure(bg=BG)
        geo = self.cfg.get("geometry")
        self.geometry(geo if geo else "480x360+60+60")
        self.minsize(300, 180)

        self._icon_path = None
        if appicon is not None:
            try:
                self._icon_path = appicon.set_app_icon(
                    APP_NAME, tk_root=self, app_id=APP_ID)
            except Exception:
                self._icon_path = None

        fs = int(self.cfg.get("font_size", 10))
        self.f_title = tkfont.Font(family="Segoe UI", size=fs + 1, weight="bold")
        self.f_head  = tkfont.Font(family="Segoe UI", size=fs - 1, weight="bold")
        self.f_msg   = tkfont.Font(family="Segoe UI", size=fs)
        self.f_sp    = tkfont.Font(family="Segoe UI", size=fs - 1, slant="italic")
        self.f_small = tkfont.Font(family="Segoe UI", size=fs - 2)
        self.f_orig  = tkfont.Font(family="Segoe UI", size=fs - 2, slant="italic")

    def _build_widgets(self):
        # title bar (drag handle)
        bar = tk.Frame(self, bg=HEADER, height=26)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Label(bar, text=f"PG Chat Translator v{APP_VERSION}", bg=HEADER,
                 fg=ACCENT, font=self.f_title).pack(side="left", padx=(8, 4))

        # target-language dropdown: what everything is translated INTO
        self._build_lang_menu(bar)

        det = "langdetect" if HAVE_LANGDETECT else "script-only"
        tk.Label(bar, text=det, bg=HEADER, fg=MUTED,
                 font=self.f_small).pack(side="left", padx=4)

        tk.Button(bar, text="x", bg=HEADER, fg=MUTED, bd=0,
                  activebackground=BAD, activeforeground="#fff",
                  font=self.f_head, command=self._quit,
                  cursor="hand2").pack(side="right", padx=(0, 6))
        self.lock_btn = tk.Button(bar, text="lock", bg=HEADER, fg=MUTED, bd=0,
                                  activebackground=PANEL, activeforeground=FG,
                                  font=self.f_small, command=self._toggle_lock,
                                  cursor="hand2")
        self.lock_btn.pack(side="right", padx=4)
        self.clr_btn = tk.Button(bar, text="clear", bg=HEADER, fg=MUTED, bd=0,
                                 activebackground=PANEL, activeforeground=FG,
                                 font=self.f_small, command=self._clear,
                                 cursor="hand2")
        self.clr_btn.pack(side="right", padx=4)
        # reverse-translate button (label shows the target foreign language)
        self.rev_btn = tk.Button(bar, text="⇄ --", bg=HEADER, fg=GOOD, bd=0,
                                 activebackground=PANEL, activeforeground=FG,
                                 font=self.f_small, command=self._do_reverse,
                                 cursor="hand2")
        self.rev_btn.pack(side="right", padx=4)
        self._apply_lock_visual()
        for w in (bar,) + tuple(bar.winfo_children()):
            if isinstance(w, tk.Label) or w is bar:
                w.bind("<Button-1>", self._start_move)
                w.bind("<B1-Motion>", self._on_move)
                w.bind("<ButtonRelease-1>", self._persist_state)

        # footer: status + resize grip. Packed BEFORE the expanding body so it
        # always keeps its slice at the bottom (otherwise the body claims the
        # whole cavity and the grip/scrollbar get cut off at the window edge).
        foot = tk.Frame(self, bg=HEADER, height=20)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        hk = self.cfg.get("reverse_hotkey", "ctrl+alt+t")
        self.status = tk.Label(foot, text=f"watching chat  ({hk} = reply)",
                               bg=HEADER, fg=MUTED, font=self.f_small,
                               anchor="w")
        self.status.pack(side="left", padx=6)
        grip = tk.Label(foot, text="⟲ drag ◢", bg=HEADER, fg=MUTED,
                        font=self.f_small, cursor="bottom_right_corner")
        grip.pack(side="right", padx=4)
        # toggle: only show the overlay while Project Gorgon is focused
        self.pg_btn = tk.Button(foot, text="", bg=HEADER, bd=0,
                                activebackground=PANEL, font=self.f_small,
                                command=self._toggle_hide, cursor="hand2")
        self.pg_btn.pack(side="right", padx=6)
        self._apply_pg_visual()
        for ev, fn in (("<Button-1>", self._start_resize),
                       ("<B1-Motion>", self._on_resize),
                       ("<ButtonRelease-1>", self._persist_state)):
            grip.bind(ev, fn)

        # body -- scrolling chat text (packed last so it fills what's left)
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        sb = tk.Scrollbar(body, command=lambda *a: self.text.yview(*a),
                          width=12, troughcolor=BG, bg=PANEL,
                          activebackground=MUTED, bd=0, highlightthickness=0)
        sb.pack(side="right", fill="y")
        self.text = tk.Text(body, bg=BG, fg=FG, bd=0, wrap="word",
                            padx=8, pady=6, state="disabled", cursor="arrow",
                            highlightthickness=0, spacing3=4, font=self.f_msg)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_config("sp",   foreground=MUTED, font=self.f_sp)
        self.text.tag_config("orig", foreground=ORIG_FG, font=self.f_orig)
        self.text.tag_config("sys",  foreground=BAD, font=self.f_small)
        self.text.tag_config("own",  foreground=MUTED, font=self.f_orig)
        self.text.tag_config("clip", foreground=GOOD, font=self.f_msg)
        self.text.tag_config("spin", foreground=ACCENT, font=self.f_msg)
        for ch, col in CHANNEL_COLORS.items():
            self.text.tag_config(f"ch_{ch}", foreground=col, font=self.f_head)

        self.bind("<Escape>", lambda e: self._quit())

    # -- output pump ---------------------------------------------------------
    def _append_line(self, ch, sp, translated, orig, lang=None):
        ch_tag = f"ch_{ch}" if ch in CHANNEL_COLORS else "sp"
        bracket = f"[{ch}·{lang}] " if lang else f"[{ch}] "
        self.text.insert("end", bracket, ch_tag)
        self.text.insert("end", f"{sp}: ", "sp")
        self.text.insert("end", translated + "\n")
        if orig:
            self.text.insert("end", f"    ↳ {orig}\n", "orig")
        self.line_count += 1

    # -- pending / spinner ---------------------------------------------------
    def _add_pending(self, pid, ch, sp, lang, orig):
        """Insert a placeholder line with an animated spinner + the captured
        original text, so you immediately see the message arrived."""
        ch_tag = f"ch_{ch}" if ch in CHANNEL_COLORS else "sp"
        tag = f"pend_{pid}"
        start = self.text.index("end-1c")
        bracket = f"[{ch}·{lang}] " if lang else f"[{ch}] "
        self.text.insert("end", bracket, ch_tag)
        self.text.insert("end", f"{sp}: ", "sp")
        spin_mark = f"spin_{pid}"
        self.text.mark_set(spin_mark, "end-1c")
        self.text.mark_gravity(spin_mark, "left")
        self.text.insert("end", self._spin[0], ("spin", tag))
        self.text.insert("end", f" {orig}\n", ("orig", tag))
        self.text.tag_add(tag, start, self.text.index("end-1c"))
        self._pending[pid] = (tag, spin_mark, ch, sp, lang)
        self.line_count += 1
        self.text.see("end")

    def _resolve_pending(self, pid, translated, orig):
        """Replace a spinner placeholder with the finished translation (or
        remove it if translated is None)."""
        info = self._pending.pop(pid, None)
        if not info:
            return
        tag, spin_mark, ch, sp, lang = info
        rng = self.text.tag_ranges(tag)
        if rng:
            start, end = rng[0], rng[-1]
            self.text.delete(start, end)
            if translated is not None:
                m = "res_tmp"
                self.text.mark_set(m, start)
                self.text.mark_gravity(m, "right")
                ch_tag = f"ch_{ch}" if ch in CHANNEL_COLORS else "sp"
                bracket = f"[{ch}·{lang}] " if lang else f"[{ch}] "
                self.text.insert(m, bracket, ch_tag)
                self.text.insert(m, f"{sp}: ", "sp")
                self.text.insert(m, translated + "\n")
                if orig:
                    self.text.insert(m, f"    ↳ {orig}\n", "orig")
                self.text.mark_unset(m)
            else:
                self.line_count = max(0, self.line_count - 1)
        try:
            self.text.mark_unset(spin_mark)
        except tk.TclError:
            pass
        self.text.see("end")

    def _spin_tick(self):
        if self._pending:
            self._spin_i = (self._spin_i + 1) % len(self._spin)
            frame = self._spin[self._spin_i]
            self.text.configure(state="normal")
            for pid, (tag, spin_mark, ch, sp, lang) in list(self._pending.items()):
                try:
                    self.text.delete(spin_mark, f"{spin_mark}+1c")
                    self.text.insert(spin_mark, frame, ("spin", tag))
                except tk.TclError:
                    pass
            self.text.configure(state="disabled")
        self.after(90, self._spin_tick)

    def _drain_out(self):
        self.text.configure(state="normal")
        try:
            while True:
                item = self.out_q.get_nowait()
                kind = item[0]
                if kind == "_line":
                    self._append_line(item[1], item[2], item[3], item[4],
                                      item[5] if len(item) > 5 else None)
                elif kind == "_pending":
                    self._add_pending(item[1], item[2], item[3], item[4], item[5])
                elif kind == "_resolve":
                    self._resolve_pending(item[1], item[2], item[3])
                elif kind == "_own":
                    self.text.insert("end", f"[{item[1]}] {item[2]} (you): "
                                            f"{item[3]}\n", "own")
                    self.line_count += 1
                elif kind == "_sys":
                    self.text.insert("end", item[1] + "\n", "sys")
                elif kind == "_clip":
                    lang, rev, orig = item[1], item[2], item[3]
                    self.clipboard_clear()
                    self.clipboard_append(rev)
                    self.text.insert("end", f"⇄ [{lang}] {rev}\n", "clip")
                    self.text.insert("end", f"    (from: {orig}) — copied to "
                                            f"clipboard\n", "orig")
                    self.line_count += 1
                    self.status.config(text=f"copied [{lang}]: {rev[:48]}")
        except queue.Empty:
            pass

        max_lines = int(self.cfg.get("max_lines", 400))
        if self.line_count > max_lines:
            self.text.delete("1.0", "80.0")
            self.line_count = max(0, self.line_count - 40)
        self.text.see("end")
        self.text.configure(state="disabled")

    def _poll(self):
        self._drain_out()
        # global-hotkey commands
        try:
            while True:
                if self.cmd_q.get_nowait() == "reverse":
                    self._do_reverse()
        except queue.Empty:
            pass
        # keep the ⇄ action button showing the effective reply target. In auto
        # mode a speaker is present, so show the channel (e.g. "⇄ es·Party");
        # a forced language has no speaker, so just show the code ("⇄ es").
        _, lang, sp, ch = self.shared.reply_target()
        if lang and lang != "??":
            ctx = ch.split(":", 1)[1] if ch and ":" in ch else ch   # Tell:Bob -> Bob
            label = f"⇄ {lang}·{ctx}" if (sp and ctx) else f"⇄ {lang}"
            self.rev_btn.config(text=label, fg=GOOD)
        else:
            self.rev_btn.config(text="⇄ --", fg=MUTED)
        self.after(120, self._poll)

    def _clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.line_count = 0

    # -- reply-language dropdown --------------------------------------------
    def _build_lang_menu(self, bar):
        cur = self.cfg.get("reply_lang")            # None = auto
        self.reply_var = tk.StringVar(value=cur or "")
        self.lang_mb = tk.Menubutton(
            bar, text=f"reply→ {cur or 'auto'} ▾", bg=HEADER, fg=ACCENT, bd=0,
            activebackground=PANEL, activeforeground=FG, font=self.f_small,
            cursor="hand2")
        menu = tk.Menu(self.lang_mb, tearoff=0, bg=HEADER, fg=FG,
                       activebackground=ACCENT, activeforeground="#ffffff",
                       bd=0)
        self.lang_mb.config(menu=menu)
        # Auto = follow whoever you're talking to (per channel).
        menu.add_radiobutton(label="Auto (match the channel)", value="",
                             variable=self.reply_var,
                             command=lambda: self._set_reply_lang(None))
        menu.add_separator()
        for label, code in COMMON_LANGS:
            menu.add_radiobutton(label=f"{label}  ({code})", value=code,
                                 variable=self.reply_var,
                                 command=lambda c=code: self._set_reply_lang(c))
        menu.add_separator()
        self.seen_menu = tk.Menu(menu, tearoff=0, bg=HEADER, fg=FG,
                                 activebackground=ACCENT,
                                 activeforeground="#ffffff", bd=0)
        menu.add_cascade(label="Seen this session", menu=self.seen_menu)
        menu.config(postcommand=self._rebuild_seen_menu)
        self.lang_mb.pack(side="left", padx=2)

    def _set_reply_lang(self, code):
        """code=None -> auto (match the channel); a code -> force that reply
        language (persists, so you can preset it before anyone speaks)."""
        code = code or None
        self.reply_var.set(code or "")
        self.lang_mb.config(text=f"reply→ {code or 'auto'} ▾")
        self.shared.set_reply_override(code)
        self.cfg["reply_lang"] = code
        self._persist_state()
        if code:
            name = LANG_NAMES.get(code, code)
            self.status.config(text=f"replies will translate into {name} ({code})")
        else:
            self.status.config(text="replies match the channel you speak in (auto)")

    def _rebuild_seen_menu(self):
        m = self.seen_menu
        m.delete(0, "end")
        seen = self.shared.seen_list()
        if not seen:
            m.add_command(label="(none seen yet)", state="disabled")
            return
        for code, count, speaker, ch in seen:
            name = LANG_NAMES.get(code, code)
            m.add_command(
                label=f"{name} ({code}) — {speaker} in {ch}  ×{count}",
                command=lambda c=code: self._set_reply_lang(c))

    # -- reverse translate ---------------------------------------------------
    def _do_reverse(self):
        txt, lang, speaker, ch = self.shared.reply_target()
        if not txt:
            self.status.config(text="no outgoing message of yours seen yet")
            return
        if not lang or lang == "??":
            self.status.config(text="pick a reply language from the dropdown, "
                                    "or wait for someone to speak")
            return
        where = ch.split(":", 1)[1] if ch and ":" in ch else ch
        ctx = f" for {where}" if speaker else ""  # speaker present => auto/channel
        self.status.config(text=f"translating your reply into [{lang}]{ctx}...")
        self.in_q.put(("reverse", txt, lang))

    # -- taskbar (borderless windows need WS_EX_APPWINDOW) -------------------
    def _enable_taskbar(self):
        if os.name != "nt":
            return
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW  = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            self._own_hwnd = hwnd
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self.withdraw()
            self.after(15, self._remap)
        except Exception:
            pass

    def _remap(self):
        try:
            self.deiconify()
            self.attributes("-topmost", True)
            if getattr(self, "_icon_path", None):
                self.iconbitmap(default=self._icon_path)
        except Exception:
            pass

    # -- show only while Project Gorgon is focused ---------------------------
    def _find_pg_hwnd(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if self._pg_hwnd and user32.IsWindow(self._pg_hwnd):
            return self._pg_hwnd
        found = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if PG_WINDOW_TITLE.lower() in buf.value.lower():
                        found.append(hwnd)
            return True

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                  wintypes.LPARAM)(cb)
        user32.EnumWindows(proc, 0)
        self._pg_hwnd = found[0] if found else None
        return self._pg_hwnd

    def _focus_tick(self):
        # Toggled off at runtime: make sure we're shown, then idle.
        if not self._hide_when_inactive:
            if not self._visible:
                self._visible = True
                self.deiconify()
                self.attributes("-topmost", True)
            self.after(400, self._focus_tick)
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not self._own_hwnd:
                self._own_hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            fg = user32.GetForegroundWindow()
            pg = self._find_pg_hwnd()
            show = (pg is not None and fg == pg) or \
                   (self._own_hwnd is not None and fg == self._own_hwnd)
            if show and not self._visible:
                self._visible = True
                self.deiconify()
                self.attributes("-topmost", True)
            elif not show and self._visible:
                self._visible = False
                self.withdraw()
        except Exception:
            pass
        self.after(400, self._focus_tick)

    # -- move / resize / lock ------------------------------------------------
    def _start_move(self, e):
        if self._locked:
            return
        self._mx, self._my = e.x, e.y

    def _on_move(self, e):
        if self._locked:
            return
        self.geometry(f"+{self.winfo_x() + e.x - self._mx}"
                      f"+{self.winfo_y() + e.y - self._my}")

    def _start_resize(self, e):
        self._rsx = self.winfo_pointerx()
        self._rsy = self.winfo_pointery()
        self._rsw = self.winfo_width()
        self._rsh = self.winfo_height()

    def _on_resize(self, e):
        w = max(self._rsw + (self.winfo_pointerx() - self._rsx), 300)
        h = max(self._rsh + (self.winfo_pointery() - self._rsy), 180)
        self.geometry(f"{int(w)}x{int(h)}")

    def _apply_pg_visual(self):
        on = self._hide_when_inactive
        self.pg_btn.config(text="PG-only ✓" if on else "PG-only ✗",
                           fg=GOOD if on else MUTED)

    def _toggle_hide(self):
        self._hide_when_inactive = not self._hide_when_inactive
        self.cfg["hide_when_pg_inactive"] = self._hide_when_inactive
        self._apply_pg_visual()
        self._persist_state()
        if not self._hide_when_inactive and not self._visible:
            self._visible = True
            self.deiconify()
            self.attributes("-topmost", True)

    def _apply_lock_visual(self):
        self.lock_btn.config(text="locked" if self._locked else "lock",
                             fg=WARN if self._locked else MUTED)

    def _toggle_lock(self):
        self._locked = not self._locked
        self._apply_lock_visual()
        self._persist_state()

    # -- state persistence ---------------------------------------------------
    def _persist_state(self, _evt=None):
        try:
            self.cfg["geometry"] = self.geometry()
            self.cfg["locked"] = self._locked
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception:
            pass

    def _check_updates(self):
        """Best-effort: tell the user (in the overlay) if a newer release exists.
        Only runs when github_repo is configured. Never blocks or raises."""
        repo = str(self.cfg.get("github_repo", "")).strip()
        if not repo or not self.cfg.get("check_updates", True):
            return

        def work():
            try:
                import urllib.request
                url = f"https://api.github.com/repos/{repo}/releases/latest"
                req = urllib.request.Request(
                    url, headers={"User-Agent": "GorgonChatTranslator"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                tag = data.get("tag_name", "")
                if tag and _version_tuple(tag) > _version_tuple(APP_VERSION):
                    self.out_q.put(("_sys", f"[update available: {tag} — "
                                            f"{data.get('html_url', '')}]"))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _quit(self):
        self._persist_state()
        self.watcher.stop()
        self.translator.stop()
        self.hotkey.stop()
        self.destroy()


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    if not os.path.isdir(LOG_DIR):
        print(f"WARNING: Chat log folder not found at:\n  {LOG_DIR}")
    if not HAVE_LANGDETECT:
        print("Tip: pip install langdetect  (needed for reverse translation "
              "and better accuracy)")
    print(f"Your language: {cfg.get('my_lang')}   Character: "
          f"{cfg.get('player_name')}   Reply hotkey: "
          f"{cfg.get('reverse_hotkey')}")
    print("Starting overlay. Drag the title bar to move, corner grip to resize.")
    ChatOverlay(cfg, Shared()).mainloop()


if __name__ == "__main__":
    main()
