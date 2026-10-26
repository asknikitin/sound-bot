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

BTN_LIST = "📋 Мої звуки"
BTN_CHECK = "🔄 Перевірити зараз"
BTN_ADD = "➕ Додати звук"
BTN_HELP = "❓ Як це працює"
BTN_FREQ = "⚙️ Налаштування"

MENU = {"keyboard": [[{"text": BTN_LIST}, {"text": BTN_CHECK}],
                     [{"text": BTN_ADD}, {"text": BTN_FREQ}],
                     [{"text": BTN_HELP}]],
        "resize_keyboard": True, "is_persistent": True}

HELP = (
    "🎵 <b>Я стежу за твоїми звуками в TikTok</b> і присилаю кожне нове відео під ними.\n\n"
    "<b>Кнопки внизу:</b>\n"
    f"{BTN_LIST} — список звуків, там же перейменувати чи видалити\n"
    f"{BTN_CHECK} — перевірити всі звуки прямо зараз\n"
    f"{BTN_ADD} — як додати новий звук\n"
    f"{BTN_FREQ} — як часто перевіряти і до скількох відео стежити за звуком\n\n"
    "Коли під звуком стає більше відео, ніж ліміт, я перестаю за ним стежити — "
    "щоб не витрачати гроші на вже розкручені звуки.\n"
    "⏳ Відповідаю не миттєво, а протягом кількох хвилин — я прокидаюсь за розкладом."
)

ADD_HOWTO = (
    "<b>Як додати звук:</b>\n"
    "1. Відкрий у TikTok будь-яке відео з твоїм звуком\n"
    "2. Тисни на крутний диск справа внизу\n"
    "3. Стрілочка «Поділитись» → «Копіювати посилання»\n"
    "4. Встав посилання сюди\n\n"
    "Кілька звуків — кожне посилання окремим повідомленням."
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
    st.setdefault("scan_hours", SCAN_EVERY_HOURS)
    st.setdefault("max_videos", 300)
    for mid, snd in st["sounds"].items():
        if snd.get("title") == mid:
            snd["title"] = ""
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- telegram

def tg(method, payload):
    for _ in range(5):
        try:
            r = requests.post(f"{TG}/{method}", json=payload, timeout=30)
        except requests.RequestException as e:
            print("telegram error:", e)
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 3) + 1)
            continue
        if not r.ok:
            print("telegram error:", method, r.status_code, r.text[:300])
        time.sleep(0.4)
        return r
    return None


def send(text, preview=False, markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": not preview}
    if markup:
        payload["reply_markup"] = markup
    tg("sendMessage", payload)


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

def name_of(mid, s):
    return s.get("title") or f"Звук {mid[-6:]}"


def send_list(st):
    items = list(st["sounds"].items())
    if not items:
        send("Поки що звуків немає.\n\n" + ADD_HOWTO, markup=MENU)
        return
    lines, buttons = [], []
    for i, (mid, s) in enumerate(items, 1):
        cnt = s.get("count") or len(s.get("seen", []))
        status = (f"понад {cnt - 1} відео, ⏸ не стежу" if s.get("paused")
                  else f"{cnt} відео, 🟢 стежу")
        lines.append(f"{i}. <b>{esc(name_of(mid, s))}</b> — {status}\n"
                     f"   https://www.tiktok.com/music/x-{mid}")
        buttons.append([{"text": f"✏️ {i}. Назвати", "callback_data": f"ren:{mid}"},
                        {"text": f"🗑 {i}. Видалити", "callback_data": f"del:{mid}"}])
    send("🎵 <b>Твої звуки:</b>\n\n" + "\n".join(lines),
         markup={"inline_keyboard": buttons})


def send_freq(st):
    h, lim = st["scan_hours"], st["max_videos"]
    vids = sum(min(x.get("count") or lim, lim + 1)
               for x in st["sounds"].values() if not x.get("paused"))
    month = vids * (24 / h) * 30 * 0.25 / 1000
    hours = [{"text": f"{'✅ ' if h == v else ''}{v:g} год", "callback_data": f"freq:{v:g}"}
             for v in (12, 24, 48)]
    limits = [{"text": f"{'✅ ' if lim == v else ''}до {v}", "callback_data": f"lim:{v}"}
              for v in (100, 300, 500, 1000)]
    send("⚙️ <b>Налаштування</b>\n\n"
         f"Перевіряю раз на <b>{h:g} год</b>\n"
         f"Стежу за звуком, поки під ним до <b>{lim} відео</b>\n\n"
         f"Зараз це ~${month:.1f}/міс в Apify (безкоштовно дають $5)\n\n"
         "Верхній ряд — як часто, нижній — до скількох відео:",
         markup={"inline_keyboard": [hours, limits]})


def handle_callback(st, cq):
    """Inline button press. Returns list of sound ids to scan now."""
    tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
    if str(cq.get("from", {}).get("id")) != CHAT_ID:
        return []
    action, _, mid = (cq.get("data") or "").partition(":")
    if action == "freq":
        st["scan_hours"] = float(mid)
        send(f"⚙️ Готово: перевіряю раз на <b>{mid} год</b>.", markup=MENU)
        return []
    if action == "lim":
        st["max_videos"] = int(mid)
        back = [k for k, x in st["sounds"].items()
                if x.get("paused") and x.get("count", 0) < int(mid)]
        for k in back:
            st["sounds"][k]["paused"] = False
        send(f"⚙️ Готово: стежу за звуками до <b>{mid} відео</b>."
             + (f"\nЗнову стежу за {len(back)} звук(ами)." if back else ""), markup=MENU)
        return back
    s = st["sounds"].get(mid)
    if not s:
        send("Цього звуку вже немає в списку.", markup=MENU)
    elif action == "ren":
        st["pending_name"] = mid
        send(f"Напиши, як назвати <b>{esc(name_of(mid, s))}</b> (наприклад: Цвіте Терен)")
    elif action == "del":
        send(f"Точно прибрати <b>{esc(name_of(mid, s))}</b>?", markup={"inline_keyboard": [[
            {"text": "Так, видалити", "callback_data": f"delok:{mid}"},
            {"text": "Ні", "callback_data": f"no:{mid}"}]]})
    elif action == "delok":
        st["sounds"].pop(mid)
        send(f"🗑 Прибрав: {esc(name_of(mid, s))}", markup=MENU)
    elif action == "no":
        send("Ок, залишаю.", markup=MENU)
    return []


def handle_commands(st):
    """Processes new Telegram updates. Returns (full_scan, [sound ids to scan])."""
    full, only = False, []
    try:
        r = requests.get(f"{TG}/getUpdates",
                         params={"offset": st["tg_offset"], "timeout": 0}, timeout=30)
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print("getUpdates failed:", e)
        return False, []

    for upd in updates:
        st["tg_offset"] = upd["update_id"] + 1
        if upd.get("callback_query"):
            only += handle_callback(st, upd["callback_query"])
            continue
        msg = upd.get("message") or upd.get("edited_message")
        if not msg or str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        pending = st.pop("pending_name", None)

        if cmd == "/start" or cmd == "/help" or text == BTN_HELP:
            send(HELP, markup=MENU)

        elif cmd == "/list" or text == BTN_LIST:
            send_list(st)

        elif cmd == "/check" or text == BTN_CHECK:
            send("🔄 Перевіряю всі звуки… Нові відео прийдуть за кілька хвилин.", markup=MENU)
            full = True

        elif cmd == "/freq" or text == BTN_FREQ:
            send_freq(st)

        elif cmd == "/add" or text == BTN_ADD:
            send(ADD_HOWTO, markup=MENU)

        elif cmd == "/name":  # old style: /name 2 Цвіте Терен
            parts = text.split(maxsplit=2)
            items = list(st["sounds"])
            if len(parts) == 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(items):
                st["sounds"][items[int(parts[1]) - 1]]["title"] = parts[2].strip()[:80]
                send(f"✏️ Готово: {esc(parts[2].strip())}", markup=MENU)
            else:
                send_list(st)

        else:
            mid = extract_music_id(text) if "tiktok" in text or text.isdigit() else None
            if mid and mid in st["sounds"]:
                send("Цей звук вже відстежую.", markup=MENU)
            elif mid:
                st["sounds"][mid] = {"title": "", "seen": [], "since": 0}
                st["pending_name"] = mid
                send("✅ <b>Додав звук!</b> Зараз подивлюсь, які відео під ним вже є.\n\n"
                     "Як його назвати? Напиши назву (наприклад: Цвіте Терен)", markup=MENU)
                only.append(mid)
            elif "tiktok" in text:
                send("Не бачу тут посилання на звук 🤔\n\n" + ADD_HOWTO, markup=MENU)
            elif pending and pending in st["sounds"] and text and not cmd:
                st["sounds"][pending]["title"] = text[:80]
                send(f"✏️ Назвав: <b>{esc(text[:80])}</b>", markup=MENU)
            else:
                send(HELP, markup=MENU)
    return full, only


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


def fetch_videos(music_id, limit=MAX_PER_SOUND):
    """Starts an Apify run, waits for it, returns all dataset items."""
    api = "https://api.apify.com/v2"
    r = requests.post(f"{api}/acts/{ACTOR}/runs", params={"token": APIFY_TOKEN},
                      json={"musicIds": [music_id], "resultsPerPage": limit},
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


def scan(st, only=None):
    errors = []
    for mid, s in list(st["sounds"].items()):
        if only is not None and mid not in only:
            continue
        if s.get("paused"):
            continue
        lim = st["max_videos"]
        try:
            raw = fetch_videos(mid, lim + 1)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name_of(mid, s)}: {e}")
            continue
        videos = [v for v in (normalize(i) for i in raw) if v]
        if not s.get("title"):
            s["title"] = next((v["title"] for v in videos if v["title"]), "")

        s["count"] = len(videos)
        if len(videos) > lim:
            s["paused"] = True
            send(f"⏸ <b>{esc(name_of(mid, s))}</b>\nПід звуком вже понад {lim} відео — "
                 "він розкрутився, далі не стежу, щоб не витрачати гроші.\n"
                 "Можна підняти ліміт у ⚙️ Налаштування.", markup=MENU)
            continue
        seen = set(s.get("seen", []))
        fresh = [v for v in videos if v["id"] not in seen]

        if not s.get("since") or s.get("v") != 2:  # first full scan: just remember
            s["since"] = int(time.time())
            s["v"] = 2
            send(f"🎵 <b>{esc(name_of(mid, s))}</b>\nБачу {len(videos)} відео, "
                 "які вже є. Далі — тільки нові.")
        else:
            # skip old videos that just showed up in the list (not really new)
            new = sorted((v for v in fresh if v["ts"] >= s["since"] - 2 * 86400),
                         key=lambda v: v["ts"])
            if 0 < len(new) <= 10:
                for v in new:
                    who = f"@{esc(v['author'])}" if v["author"] else "хтось"
                    send(f"🆕 <b>{esc(name_of(mid, s))}</b>\n{who} · 👁 {fmt_views(v['views'])}\n"
                         f"{esc(v['caption'])}\n{v['url']}", preview=True)
            elif new:
                lines = [f"• @{esc(v['author'] or '?')} · 👁 {fmt_views(v['views'])}\n{v['url']}"
                         for v in new]
                header = f"🆕 <b>{esc(name_of(mid, s))}</b> — {len(new)} нових відео\n\n"
                chunk = header
                for line in lines:
                    if len(chunk) + len(line) > 3800:
                        send(chunk)
                        chunk = ""
                    chunk += line + "\n"
                if chunk.strip():
                    send(chunk)

        s["seen"] = (s.get("seen", []) + [v["id"] for v in fresh])[-SEEN_CAP:]

    if only is None:
        st["last_scan"] = int(time.time())
    if errors:
        send("⚠️ Не вдалось перевірити:\n" + esc("\n".join(errors))[:3000])


# ---------------------------------------------------------------- main

def main():
    st = load_state()
    if st.get("menu_v") != 2:  # the "Menu" button next to the message box
        tg("setMyCommands", {"commands": [
            {"command": "list", "description": "Мої звуки"},
            {"command": "check", "description": "Перевірити зараз"},
            {"command": "add", "description": "Як додати звук"},
            {"command": "freq", "description": "Частота перевірки"},
            {"command": "help", "description": "Як це працює"}]})
        st["menu_v"] = 2
    full, only = handle_commands(st)
    save_state(st)

    due = time.time() - st["last_scan"] >= st["scan_hours"] * 3600 - 600
    if st["sounds"] and (full or FORCE_SCAN or due):
        scan(st)
        save_state(st)
    elif only:
        scan(st, only=only)
        save_state(st)


if __name__ == "__main__":
    main()
