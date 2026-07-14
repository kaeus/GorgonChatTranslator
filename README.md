# Gorgon Chat Translator

An always-on-top, draggable, resizable dark overlay for **Project Gorgon** that
live-translates foreign-language chat into your language — and helps you reply
back in theirs.

It tails the newest chat log, ignores messages already in your language (so it
barely touches any translation API), and shows translations of the rest. Press
a hotkey and it translates *your* last message into the last foreign language
someone used and copies it to the clipboard.

![overlay](docs/screenshot.png)

---

## Features

- **Live overlay** — tails `…/Project Gorgon/ChatLogs/Chat-*.log`, auto-switches
  at the daily file rollover, never locks the file.
- **Channel blacklist** — watches every channel *except* the ones you list
  (default: `Combat`, `Status`, `NPC Chatter`, `Error`).
- **Only translates foreign text** — English (or whatever your language is) is
  detected and skipped, which is what keeps API usage tiny.
- **Two-way replies** — a hotkey (default **Ctrl+Alt+T**) or the **⇄** button
  translates your most recent outgoing message into the last foreign language
  seen **on the channel you last spoke in**, and copies it to the clipboard to
  paste into game chat. The reply target is scoped per channel, so a different
  language used in Global won't hijack a reply you're composing in Party — the
  **⇄** button shows the current target like `⇄ es·Party`.
- **Tell-aware** — `[Tell] Bob->You` and `[Tell] You->Bob` are parsed as a
  conversation with *Bob*: incoming tells are translated and shown as
  `[Tell] Bob: …`, your outgoing tells are recognized as your own messages, and
  the reply language is scoped **per person** (`Tell:Bob`), so replying to a
  Chinese-speaking tell won't pick up the language of a different tell
  conversation.
- **Robust CJK handling** — Google's `auto` detection sometimes fails to
  translate short Chinese/Japanese/Korean text; when it returns the line
  unchanged, the tool retries with a source language derived from the Unicode
  script, and never silently drops a clearly-foreign message.
- **Reply-language dropdown** — the **reply→ auto ▾** button in the title bar
  sets the language your *own* messages are translated *into* when you hit the
  reverse hotkey:
  - **Auto (match the channel)** — default; replies use the last foreign
    language seen on the channel you last spoke in.
  - **A specific language** — force it (persists), so you can preset a reply
    language *before* anyone speaks — e.g. set Spanish, then greet a guildmate
    in Spanish immediately.
  - **Seen this session** submenu — quick-pick any language that has appeared in
    chat (with who used it, where, and how many times).
- **Per-line language tag** — each translated line shows the detected source
  language in its bracket, e.g. `[Tell·zh-CN] Bob: …` or `[Global·es] Diego: …`
  (English lines just show `[Global]`).
- **Live spinner** — the moment a foreign message is captured it appears with an
  animated spinner and the original text, which is replaced in place by the
  translation once it returns — so you always see that something was caught,
  even during a throttled burst.
- **Overlay niceties** — resize grip, move by dragging the title bar, **lock**
  to prevent accidental moves, **clear**, adjustable opacity, its own taskbar
  icon, and a **PG-only** toggle in the footer to turn the "only show while
  Project Gorgon is focused" behaviour on/off without editing the config.
- Size / position / lock state persist to `chat_translator_config.json`.

---

## Install & run

```sh
pip install -r requirements.txt
python chat_translator.py
```

`langdetect` is technically optional, but **install it** — without it the
reverse-reply feature can't know which language to reply in, and only non-Latin
scripts (Russian, Chinese, …) are detected as foreign.

### Rate limits

The free `GoogleTranslator` backend has no published limit, but sustained
bursts of hundreds of calls earn a temporary IP block. This tool stays well
clear by (1) never translating your own language, (2) caching
original→translation, (3) throttling to `min_api_interval` seconds per call, and
(4) backing off on errors. On an English server that's a handful of calls per
minute at most.

---

## First-run setup

On first launch it writes `chat_translator_config.json`. Set at least:

- **`player_name`** — your character name, so it can find *your* messages for
  the reverse-reply feature.
- **`my_lang`** — the language you read/speak (`en`, `es`, `fr`, …). Foreign
  chat is translated *into* it; your replies are translated *from* it.

## Configuration (`chat_translator_config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `player_name` | `"Kaeus"` | Your character; identifies your outgoing lines. |
| `my_lang` | `"en"` | Your language (incoming chat translates into it / replies translate from it). |
| `reply_lang` | `null` | Language replies translate *into*. `null` = auto (match the channel); a code (e.g. `"es"`) forces it. Set from the dropdown. |
| `blacklist_channels` | `["Combat","Status","NPC Chatter","Error"]` | Channels to ignore. Everything else is shown. |
| `reverse_hotkey` | `"ctrl+alt+t"` | Global hotkey to translate + copy your reply. |
| `min_api_interval` | `0.40` | Min seconds between translation API calls. |
| `min_len` | `4` | Ignore messages shorter than this (unreliable to detect). |
| `show_english` | `false` | Also show messages already in your language. |
| `show_own` | `false` | Echo your own outgoing messages (dimmed). |
| `max_lines` | `400` | Trim overlay history to this many lines. |
| `opacity` | `0.92` | Window opacity, 0–1. |
| `font_size` | `10` | Base font size. |
| `hide_when_pg_inactive` | `false` | Only show while Project Gorgon is focused (toggle in the footer). |
| `github_repo` | `""` | `"owner/repo"` to enable the in-app "update available" notice. Empty = off. |
| `check_updates` | `true` | Whether to check GitHub for a newer release on start. |
| `locked` | `false` | Remembered lock state. |
| `geometry` | `null` | Remembered size + position. |

`reverse_hotkey` accepts combos like `ctrl+alt+t`, `shift+f9`, `ctrl+win+r`.

---

## Reply workflow

1. A Spanish speaker writes in Party → the overlay shows the English
   translation and remembers `es` for the **Party** channel (the **⇄** button
   shows `⇄ es·Party`). Chatter in other channels/languages is tracked
   separately and won't change this.
2. Type your reply in game chat (in Party) as normal and send it.
3. Press **Ctrl+Alt+T** (or click the **⇄** button) → your message is
   translated to Spanish (the language seen on Party, the channel you last
   spoke in) and copied to the clipboard.
4. Paste it into game chat and send.

---

## Building a standalone .exe

No Python needed on the target machine:

```sh
build_exe.bat
```

Produces `dist\GorgonChatTranslator.exe`. The build is self-contained — it uses
the vendored `appicon.py`, generates `app.ico` via `gen_icon.py`, and builds
with `GorgonChatTranslator.spec`, which bundles `langdetect`'s language-profile
data via `collect_all` (a bare `--onefile` command would omit those and crash at
runtime) and embeds `version.txt` so the app knows its own version.

---

## Versioning & auto-published releases

The app reads its version from `version.txt` and shows it in the title bar
(`PG Chat Translator v1.0.42`). A GitHub Actions workflow
(`.github/workflows/release.yml`) builds and publishes automatically:

- On every push to `main` (docs-only changes are skipped) or a manual **Run
  workflow**, a Windows runner builds the exe.
- The version **auto-increments** as `1.0.<github-run-number>`, is stamped into
  `version.txt` before building, and becomes the release tag `v1.0.<n>`.
- The exe is uploaded to a **GitHub Release**, which GitHub always surfaces as
  **Latest** — so `…/releases/latest` is your always-current download.

**One-time setup** to turn this repo into that pipeline:

```sh
cd GorgonChatTranslator
git init && git add . && git commit -m "initial"
gh repo create GorgonChatTranslator --public --source=. --push
```

No secrets needed — the workflow uses the built-in `GITHUB_TOKEN`. To get the
in-app "update available" notice, set `github_repo` in the config to your
`owner/repo` (e.g. `"kaeus/GorgonChatTranslator"`).

---

## Notes / limitations

- On very short lines (2–3 words) `langdetect` can mislabel the language (e.g.
  Spanish as Catalan). Translation still works; the reply target may be a close
  neighbour language.
- English game jargon (`LFM`, `WTS`, …) and common function words are treated as
  English via a built-in word list, so they're never sent to the API.
- Global-hotkey support is Windows-only (`RegisterHotKey`). The overlay itself
  is Tkinter/stdlib and runs elsewhere, just without the global hotkey.