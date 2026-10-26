"""
TikTok sound watcher -> Telegram.

Runs on GitHub Actions on a schedule. Each run:
  1. reads new Telegram messages (/add, /list, /remove, /check, /help)
  2. every SCAN_EVERY_HOURS hours (or on /check) asks Apify for the videos
     under each tracked sound and sends only the new ones to Telegram
  3. saves everything to state.json (the workflow commits it back to the repo)
"""

import html
import json
import os
import re
import time

import requests

TG_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
APIFY_TOKEN = os.environ["APIFY_TOKEN"].strip()

ACTOR = os.environ.get("APIFY_ACTOR", "dami_studio~tiktok-music-scraper")
MAX_PER_SOUND = int(os.environ.get("MAX_VIDEOS", "2000"))
SCAN_EVERY_HOURS = float(os.environ.get("SCAN_HOURS", "24"))
FORCE_SCAN = os.environ.get("FORCE_SCAN", "0") == "1"

STATE_FILE = "state.json"
SEEN_CAP = 5000  # how many video ids to remember per sound
TG = f"https://api.telegram.org/bot{TG_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

HELP = (
    "🎵 <b>Бот стежить за твоїми звуками в TikTok</b>\n\n"
    "Просто кинь посилання на звук (сторінка /music/...) — і я його додам.\n\n"
    "/list — список звуків\n"
    "/remove 2 — прибрати звук №2\n"
    "/name 2 Цвіте Терен — назвати звук №2\n"
    "/check — перевірити зараз\n"
    "/help — ця підказка\n\n"
    f"Перевіряю нові відео кожні {SCAN_EVERY_HOURS:g} год."
)


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("tg_offset", 0)
    st.setdefault("sounds", {})
    st.setdefault("last_scan", 0)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- telegram

def send(text, preview=False):
    for _ in range(5):
        r = requests.post(f"{TG}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
            continue
        if not r.ok:
            print("telegram error:", r.status_code, r.text[:300])
        time.sleep(0.4)
        return


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ---------------------------------------------------------------- links

MUSIC_RE = re.compile(r"tiktok\.com/music/[^\s?#]*?(\d{10,})")
URL_RE = re.compile(r"https?://\S+")


def extract_music_id(text):
    """Returns the TikTok music id from a message, or None."""
    m = MUSIC_RE.search(text)
    if m:
        return m.group(1)
    for url in URL_RE.findall(text):
        if "tiktok.com" not in url:
            continue
        try:  # short share links: vm.tiktok.com/..., tiktok.com/t/...
            final = requests.get(url, headers=UA, allow_redirects=True, timeout=20).url
        except requests.RequestException:
            continue
        m = MUSIC_RE.search(final)
        if m:
            return m.group(1)
    bare = re.fullmatch(r"\s*(\d{15,})\s*", text)
    return bare.group(1) if bare else None


# ---------------------------------------------------------------- commands

def sound_list(st):
    return list(st["sounds"].items())


def handle_commands(st):
    """Processes new Telegram messages. Returns True if a scan should run now."""
    force = False
    try:
        r = requests.get(f"{TG}/getUpdates",
                         params={"offset": st["tg_offset"], "timeout": 0}, timeout=30)
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print("getUpdates failed:", e)
        return False

    for upd in updates:
        st["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("edited_message")
        if not msg or str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text else ""

        if cmd in ("/start", "/help"):
            send(HELP)

        elif cmd == "/list":
            items = sound_list(st)
            if not items:
                send("Поки що звуків немає. Кинь посилання на звук.")
            else:
                lines = [f"{i}. {esc(s.get('title') or mid)} — {len(s.get('seen', []))} відео"
                         for i, (mid, s) in enumerate(items, 1)]
                send("🎵 <b>Звуки:</b>\n" + "\n".join(lines))

        elif cmd == "/remove":
            arg = text[len("/remove"):].strip()
            items = sound_list(st)
            mid = None
            if arg.isdigit() and 1 <= int(arg) <= len(items):
                mid = items[int(arg) - 1][0]
            elif arg in st["sounds"]:
                mid = arg
            if mid:
                title = st["sounds"].pop(mid).get("title") or mid
                send(f"Прибрав: {esc(title)}")
            else:
                send("Вкажи номер зі списку: /remove 2")

        elif cmd == "/name":
            parts = text.split(maxsplit=2)
            items = sound_list(st)
            if len(parts) == 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(items):
                mid = items[int(parts[1]) - 1][0]
                st["sounds"][mid]["title"] = parts[2].strip()[:80]
                send(f"Звук №{parts[1]} тепер: {esc(parts[2].strip())}")
            else:
                send("Напиши так: /name 2 Цвіте Терен")

        elif cmd == "/check":
            send("Перевіряю…")
            force = True

        else:
            mid = extract_music_id(text)
            if mid and mid in st["sounds"]:
                send("Цей звук вже відстежую.")
            elif mid:
                st["sounds"][mid] = {"title": "", "seen": [], "since": 0}
                send("✅ Додав звук. Зараз запам'ятаю, що під ним вже є, "
                     "а далі присилатиму тільки нові відео.")
                force = True
            elif text.startswith("/add") or "tiktok.com" in text:
                send("Не бачу тут посилання на звук. Відкрий відео → тисни на "
                     "крутний диск справа внизу → поділись посиланням на звук.")
            else:
                send(HELP)
    return force


# ---------------------------------------------------------------- apify

def pick(d, *paths):
    for path in paths:
        cur = d
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def fetch_videos(music_id):
    """Starts an Apify run, waits for it, returns all dataset items."""
    api = "https://api.apify.com/v2"
    r = requests.post(f"{api}/acts/{ACTOR}/runs", params={"token": APIFY_TOKEN},
                      json={"musicIds": [music_id], "resultsPerPage": MAX_PER_SOUND},
                      timeout=60)
    r.raise_for_status()
    run = r.json()["data"]
    deadline = time.time() + 8 * 60
    while run["status"] in ("READY", "RUNNING") and time.time() < deadline:
        r = requests.get(f"{api}/actor-runs/{run['id']}",
                         params={"token": APIFY_TOKEN, "waitForFinish": 60}, timeout=90)
        r.raise_for_status()
        run = r.json()["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run['status']}")
    items, offset = [], 0
    while True:
        r = requests.get(f"{api}/datasets/{run['defaultDatasetId']}/items",
                         params={"token": APIFY_TOKEN, "offset": offset, "limit": 1000,
                                 "clean": "true"}, timeout=120)
        r.raise_for_status()
        page = r.json()
        items += page
        if len(page) < 1000:
            return items
        offset += 1000


def normalize(item):
    vid = pick(item, "id", "videoId", "aweme_id", "awemeId", "video_id")
    if not vid or not str(vid).isdigit():
        return None
    vid = str(vid)
    author = pick(item, "authorMeta.name", "author.uniqueId", "author.unique_id",
                  "authorUsername", "authorName", "author")
    author = author if isinstance(author, str) else None
    url = pick(item, "webVideoUrl", "url", "shareUrl")
    if not (isinstance(url, str) and "/video/" in url):
        url = (f"https://www.tiktok.com/@{author}/video/{vid}" if author
               else f"https://m.tiktok.com/v/{vid}.html")
    views = pick(item, "playCount", "stats.playCount", "statistics.play_count",
                 "statistics.playCount", "views", "viewCount")
    caption = pick(item, "text", "desc", "description", "caption") or ""
    title = pick(item, "musicMeta.musicName", "music.title", "musicTitle",
                 "soundTitle", "music.name")
    return {
        "id": vid,
        "ts": int(vid) >> 32,  # TikTok ids contain the upload time
        "url": url,
        "author": author,
        "views": views,
        "caption": str(caption)[:120],
        "title": title if isinstance(title, str) else None,
    }


def fmt_views(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def scan(st):
    errors = []
    for mid, s in list(st["sounds"].items()):
        try:
            raw = fetch_videos(mid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s.get('title') or mid}: {e}")
            continue
        videos = [v for v in (normalize(i) for i in raw) if v]
        if not s.get("title"):
            s["title"] = next((v["title"] for v in videos if v["title"]), "") or mid

        seen = set(s.get("seen", []))
        fresh = [v for v in videos if v["id"] not in seen]

        if not s.get("since") or s.get("v") != 2:  # first full scan: just remember
            s["since"] = int(time.time())
            s["v"] = 2
            send(f"🎵 <b>{esc(s['title'])}</b>\nБачу {len(videos)} відео, "
                 "які вже є. Далі — тільки нові.")
        else:
            # skip old videos that just showed up in the list (not really new)
            new = sorted((v for v in fresh if v["ts"] >= s["since"] - 2 * 86400),
                         key=lambda v: v["ts"])
            if 0 < len(new) <= 10:
                for v in new:
                    who = f"@{esc(v['author'])}" if v["author"] else "хтось"
                    send(f"🆕 <b>{esc(s['title'])}</b>\n{who} · 👁 {fmt_views(v['views'])}\n"
                         f"{esc(v['caption'])}\n{v['url']}", preview=True)
            elif new:
                lines = [f"• @{esc(v['author'] or '?')} · 👁 {fmt_views(v['views'])}\n{v['url']}"
                         for v in new]
                header = f"🆕 <b>{esc(s['title'])}</b> — {len(new)} нових відео\n\n"
                chunk = header
                for line in lines:
                    if len(chunk) + len(line) > 3800:
                        send(chunk)
                        chunk = ""
                    chunk += line + "\n"
                if chunk.strip():
                    send(chunk)

        s["seen"] = (s.get("seen", []) + [v["id"] for v in fresh])[-SEEN_CAP:]

    st["last_scan"] = int(time.time())
    if errors:
        send("⚠️ Не вдалось перевірити:\n" + esc("\n".join(errors))[:3000])


# ---------------------------------------------------------------- main

def main():
    st = load_state()
    force = handle_commands(st)
    save_state(st)

    due = time.time() - st["last_scan"] >= SCAN_EVERY_HOURS * 3600 - 600
    if st["sounds"] and (force or FORCE_SCAN or due):
        scan(st)
        save_state(st)


if __name__ == "__main__":
    main()
