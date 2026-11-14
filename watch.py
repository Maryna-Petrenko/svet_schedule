#!/usr/bin/env python3
"""
Стежить за публічним Telegram-каналом Чернігівобленерго і пересилає у вашу групу
інформацію ТІЛЬКИ про вашу чергу (за замовчуванням 4.1).

Як визначається, чи стосується пост вашої черги:
  1) Текстовий пост зі списком черг  -> надсилаємо лише рядки про вашу чергу;
     зміни в інших чергах ігноруються (порівнюється тільки "ваша" частина).
  2) Пост-картинка з графіком        -> картинку "читає" Gemini (потрібен GEMINI_API_KEY),
     витягує періоди для вашої черги; повідомлення йде лише якщо вони нові/змінились.
  3) Загальні тексти про графіки (напр. "додаткові відключення") -> надсилаються,
     якщо ALSO_GENERAL=1 (за замовчуванням так).

Змінні оточення:
  BOT_TOKEN          токен бота від @BotFather                    (обов'язково)
  CHAT_ID            id групи, напр. -4218775853                  (обов'язково)
  QUEUE              ваша черга, за замовчуванням 4.1
  GEMINI_API_KEY     ключ Google Gemini для розпізнавання картинок (необов'язково)
  GEMINI_MODEL       модель, за замовчуванням gemini-3.5-flash (запасна: gemini-2.5-flash)
  UNCERTAIN          send|skip: що робити, якщо чергу в картинці не вдалось визначити (send)
  ALSO_GENERAL       1|0: пересилати загальні пости про графіки (1)
  CHANNEL            username каналу без @ (chernigivoblenergo)
  KEYWORDS           слова-фільтри через кому
  STATE_FILE         файл стану (state.json)
  SEND_LAST          "1": на першому запуску надіслати останній відповідний пост (тест)
"""
import base64
import datetime
import hashlib
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
QUEUE = os.environ.get("QUEUE", "4.1").strip()
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
UNCERTAIN = os.environ.get("UNCERTAIN", "send")
ALSO_GENERAL = os.environ.get("ALSO_GENERAL", "1") == "1"
CHANNEL = os.environ.get("CHANNEL", "chernigivoblenergo").lstrip("@")
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get(
        "KEYWORDS", "графік,графіки,гпв,гав,черг,погодинн,стабілізацій"
    ).split(",")
    if k.strip()
]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SEND_LAST = os.environ.get("SEND_LAST") == "1"
KEEP = 100
UA = {"User-Agent": "Mozilla/5.0 (compatible; outage-family-bot/1.0)"}

_a, _b = re.split(r"[.,/]", QUEUE)
MY_QUEUE_RE = re.compile(rf"(?<![\d.,/]){_a}\s*[./,]\s*{_b}(?!\d)")
ANY_QUEUE_RE = re.compile(r"(?<![\d.,/])[1-9]\s*[./,]\s*[1-9](?!\d)")


# ---------- Telegram-канал ----------
def fetch_posts():
    r = requests.get(f"https://t.me/s/{CHANNEL}", headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for w in soup.select("div.tgme_widget_message[data-post]"):
        data_post = w["data-post"]
        try:
            pid = int(data_post.rsplit("/", 1)[-1])
        except ValueError:
            continue
        t = w.select_one("div.tgme_widget_message_text")
        text = t.get_text("\n", strip=True) if t else ""
        photo = None
        ph = w.select_one("a.tgme_widget_message_photo_wrap")
        if ph and ph.get("style"):
            m = re.search(r"url\(['\"]?([^'\")]+)", ph["style"])
            if m:
                photo = m.group(1)
        posts.append({"id": pid, "text": text, "photo": photo,
                      "link": f"https://t.me/{data_post}"})
    posts.sort(key=lambda p: p["id"])
    return posts


def keyword_match(post):
    low = post["text"].lower()
    return any(k in low for k in KEYWORDS)


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------- Класифікація поста ----------
def classify(post):
    """-> (kind, digest, relevant_text)
    kind: queue_text | image | general"""
    text = post["text"]
    queue_mentions = ANY_QUEUE_RE.findall(text)
    if len(set(m.replace(",", ".").replace("/", ".").replace(" ", "") for m in queue_mentions)) >= 2:
        lines = [l for l in text.split("\n") if any(c.isalpha() for c in l)]
        mine = [l for l in lines if MY_QUEUE_RE.search(l)]
        if not mine:
            return "queue_text", h(""), ""
        rel = (lines[0] + "\n" if not MY_QUEUE_RE.search(lines[0]) else "") + "\n".join(mine)
        return "queue_text", h(rel), rel
    if post["photo"]:
        return "image", h(text), text
    return "general", h(text), text


# ---------- Розпізнавання картинки через Gemini ----------
def get_image(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    mt = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if mt not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mt = "image/jpeg"
    return r.content, mt


def read_my_queue(img, media_type):
    """Повертає {"found":bool,"date":"YYYY-MM-DD"|None,"outages":[{"from","to"}]} або None."""
    if not API_KEY:
        return None
    today = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d")
    prompt = (
        "Це картинка з графіком погодинних відключень електроенергії (Чернігівобленерго): "
        "сітка карток, кожна для черги/підчерги (1.1, 1.2, 2.1 ... 6.2); під номером картки "
        "перелічені інтервали, коли світла НЕ буде (біля значка блискавки). "
        f"Сьогодні {today}. Знайди картку {QUEUE} (може бути записано як {QUEUE}, "
        f"{QUEUE.replace('.', ',')} або {QUEUE.replace('.', '/')}). Дату графіка візьми із заголовка картинки. "
        "Поверни ЛИШЕ JSON без пояснень і без ```:\n"
        '{"found": true|false, "date": "YYYY-MM-DD"|null, '
        '"outages": [{"from": "HH:MM", "to": "HH:MM"}]}\n'
        "У outages - тільки інтервали відключення саме з картки " + QUEUE + ", точно як на картинці; "
        'кінець опівночі запиши як "24:00". Якщо в картці інтервалів немає - outages=[]. '
        f"Якщо картки {QUEUE} на картинці немає або це не графік черг - found=false."
    )
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": media_type,
                             "data": base64.b64encode(img).decode()}},
            {"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": API_KEY, "content-type": "application/json"},
        json=body, timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    txt = "".join(p_.get("text", "") for p_ in parts if not p_.get("thought"))
    txt = re.sub(r"```(?:json)?", "", txt).strip()
    return json.loads(txt)


SEP = "~" * 22


def summarize(res, link):
    """Текст у стилі бота обленерго: Початок / Закінчення."""
    try:
        d = datetime.date.fromisoformat(res.get("date"))
    except Exception:
        d = None
    outages = res.get("outages") or []
    if not outages:
        when = d.strftime("%d-%m-%Y") if d else "вказану дату"
        body = f"Відключень для черги {QUEUE} на {when} не заплановано."
    else:
        blocks = []
        for o in outages:
            f, t = o["from"], o["to"]
            end_d, end_t = d, t
            if t in ("24:00", "00:00") or t <= f:
                end_d = d + datetime.timedelta(days=1) if d else None
                end_t = "00:00" if t in ("24:00", "00:00") else t
            ds = d.strftime("%d-%m-%Y ") if d else ""
            de = end_d.strftime("%d-%m-%Y ") if end_d else ""
            blocks.append(f"Початок: {ds}{f}\nЗакінчення: {de}{end_t}")
        body = f"Графік погодинних відключень (ГПВ)\n" + ("\n" + SEP + "\n").join(blocks) + "\n" + SEP
    return (f"Черга {QUEUE}\n{body}\n\n"
            f"ℹ️ Розпізнано з картинки каналу (Gemini), звіряйте з оригіналом: {link}")


# ---------- Telegram-бот ----------
def tg(method, **kwargs):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, timeout=60, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data


def send(text, photo_bytes=None):
    text = text[:3900]
    if photo_bytes:
        cap = text if len(text) <= 1000 else text[:1000]
        tg("sendPhoto", data={"chat_id": CHAT_ID, "caption": cap},
           files={"photo": ("graph.jpg", photo_bytes)})
        if len(text) > 1000:
            tg("sendMessage", data={"chat_id": CHAT_ID, "text": text,
                                     "disable_web_page_preview": True})
    else:
        tg("sendMessage", data={"chat_id": CHAT_ID, "text": text,
                                 "disable_web_page_preview": True})


# ---------- Стан ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(seen, q):
    keep = dict(sorted(seen.items(), key=lambda kv: int(kv[0]))[-KEEP:])
    q = dict(sorted(q.items())[-14:])
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": keep, "q": q}, f, ensure_ascii=False, indent=1)


# ---------- Обробка одного поста ----------
def process(post, kind, rel, updated, qstate):
    """Надсилає повідомлення за потреби. Повертає True, якщо щось надіслано."""
    head = "🔄 Оновлено: " if updated else "⚡ "
    if kind == "queue_text":
        if not rel:
            return False
        send(f"{head}Чернігівобленерго, черга {QUEUE}\n\n{rel}\n\nДжерело: {post['link']}")
        return True

    if kind == "image":
        img, mt = get_image(post["photo"])
        res = None
        try:
            res = read_my_queue(img, mt)
        except Exception as e:
            print(f"Не вдалось розпізнати картинку поста {post['id']}: {e}", file=sys.stderr)
        if res and res.get("found"):
            key = res.get("date") or f"post{post['id']}"
            ph = h(json.dumps(res.get("outages") or [], sort_keys=True))
            prev = qstate.get(key)
            if prev == ph:
                return False  # для вашої черги нічого не змінилось
            msg = summarize(res, post["link"])
            if prev is not None:
                msg = "🔄 Змінилось!\n" + msg
            send(msg)
            qstate[key] = ph
            return True
        if UNCERTAIN == "send":
            why = "ключ Gemini не задано" if not API_KEY else f"не знайшов чергу {QUEUE} на картинці"
            send(f"{head}Чернігівобленерго (картинка; {why}, перегляньте самі)\n\n"
                 f"{post['text']}\n\nДжерело: {post['link']}", img)
            return True
        return False

    # general
    if ALSO_GENERAL:
        send(f"{head}Чернігівобленерго\n\n{post['text']}\n\nДжерело: {post['link']}")
        return True
    return False


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("Потрібні BOT_TOKEN і CHAT_ID")

    posts = fetch_posts()
    if not posts:
        sys.exit("Не знайшов жодного поста: перевірте CHANNEL або доступність t.me/s/…")

    state = load_state()
    first_run = state is None
    seen = {} if first_run else state.get("seen", {})
    qstate = {} if first_run else state.get("q", {})

    if first_run:
        hit = None
        for p in posts:
            kind, dg, rel = classify(p)
            seen[str(p["id"])] = dg
            if keyword_match(p):
                hit = (p, kind, rel)
        if SEND_LAST and hit:
            process(hit[0], hit[1], hit[2], False, qstate)
        save_state(seen, qstate)
        print(f"Перший запуск: запамʼятав {len(posts)} постів.")
        return

    sent = 0
    for p in posts:
        key = str(p["id"])
        kind, dg, rel = classify(p)
        old = seen.get(key)
        if old == dg:
            continue
        if keyword_match(p) or kind == "queue_text":
            try:
                if process(p, kind, rel, old is not None, qstate):
                    sent += 1
            except Exception as e:  # стан не оновлюємо - повторимо наступного разу
                print(f"Помилка поста {p['id']}: {e}", file=sys.stderr)
                continue
        seen[key] = dg
    save_state(seen, qstate)
    print(f"Готово. Надіслано: {sent}.")


if __name__ == "__main__":
    main()
