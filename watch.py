#!/usr/bin/env python3
"""
Стежить за публічним Telegram-каналом Чернігівобленерго і пересилає у вашу групу
інформацію ТІЛЬКИ про вашу чергу (за замовчуванням 4.1).

Як визначається, чи стосується пост вашої черги:
  1) Текстовий пост зі списком черг  -> надсилаємо лише рядки про вашу чергу;
     зміни в інших чергах ігноруються (порівнюється тільки "ваша" частина).
  2) Пост-картинка з графіком        -> картинку "читає" Claude (потрібен ANTHROPIC_API_KEY),
     витягує періоди для вашої черги; повідомлення йде лише якщо вони нові/змінились.
  3) Загальні тексти про графіки (напр. "додаткові відключення") -> надсилаються,
     якщо ALSO_GENERAL=1 (за замовчуванням так).

Змінні оточення:
  BOT_TOKEN          токен бота від @BotFather                    (обов'язково)
  CHAT_ID            id групи, напр. -4218775853                  (обов'язково)
  QUEUE              ваша черга, за замовчуванням 4.1
  ANTHROPIC_API_KEY  ключ для розпізнавання картинок (необов'язково)
  MODEL              модель Claude, за замовчуванням claude-sonnet-5-5
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
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
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

_a, _b = re.split(r"[.,]", QUEUE)
MY_QUEUE_RE = re.compile(rf"(?<![\d.,]){_a}\s*[.,]\s*{_b}(?!\d)")
ANY_QUEUE_RE = re.compile(r"(?<![\d.,])[1-9]\s*[.,]\s*[1-9](?!\d)")


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
    if len(set(m.replace(",", ".").replace(" ", "") for m in queue_mentions)) >= 2:
        lines = [l for l in text.split("\n") if l.strip()]
        mine = [l for l in lines if MY_QUEUE_RE.search(l)]
        if not mine:
            return "queue_text", h(""), ""
        rel = (lines[0] + "\n" if not MY_QUEUE_RE.search(lines[0]) else "") + "\n".join(mine)
        return "queue_text", h(rel), rel
    if post["photo"]:
        return "image", h(text), text
    return "general", h(text), text


# ---------- Розпізнавання картинки через Claude ----------
def get_image(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    mt = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if mt not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mt = "image/jpeg"
    return r.content, mt


def read_my_queue(img, media_type):
    """Повертає {"found":bool,"date":"YYYY-MM-DD"|None,"periods":[...]} або None."""
    if not API_KEY:
        return None
    today = datetime.datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d")
    prompt = (
        f"Це графік відключень електроенергії (Чернігівобленерго). Сьогодні {today}. "
        f"Знайди рядок для черги/підчерги {QUEUE} (може бути записано як {QUEUE}, "
        f"{QUEUE.replace('.', ',')} або 'черга {_a} підчерга {_b}'). Зазвичай кольори: "
        "чорний = світла немає, синій/сірий = можливе відключення, зелений/білий = світло є "
        "(звір з легендою на картинці). Поверни ЛИШЕ JSON без пояснень і без ```:\n"
        '{"found": true|false, "date": "YYYY-MM-DD"|null, '
        '"periods": [{"from": "HH:MM", "to": "HH:MM", "status": "off"|"maybe"|"on"}]}\n'
        "Періоди мають покривати добу послідовно, сусідні з однаковим статусом об'єднай. "
        f'Якщо черги {QUEUE} на картинці немає або картинка не є графіком черг - found=false, periods=[].'
    )
    body = {
        "model": MODEL,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                          "data": base64.b64encode(img).decode()}},
            {"type": "text", "text": prompt}]}],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=body, timeout=120)
    r.raise_for_status()
    txt = "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text")
    txt = re.sub(r"```(?:json)?", "", txt).strip()
    return json.loads(txt)


EMOJI = {"off": "🔴", "maybe": "🟡", "on": "🟢"}
LABEL = {"off": "світла немає", "maybe": "можливе відключення", "on": "світло є"}


def summarize(res, link):
    d = res.get("date")
    try:
        d = datetime.date.fromisoformat(d).strftime("%d.%m.%Y")
    except Exception:
        d = "дата не визначена"
    lines = [f"{EMOJI.get(p['status'], '•')} {p['from']}–{p['to']} — {LABEL.get(p['status'], p['status'])}"
             for p in res["periods"]]
    return (f"Графік для черги {QUEUE} на {d}\n\n" + "\n".join(lines) +
            f"\n\n⚠️ Розпізнано автоматично з картинки, звіряйте з оригіналом.\nДжерело: {link}")


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
        if res and res.get("found") and res.get("periods"):
            key = res.get("date") or f"post{post['id']}"
            ph = h(json.dumps(res["periods"], sort_keys=True))
            prev = qstate.get(key)
            if prev == ph:
                return False  # для вашої черги нічого не змінилось
            msg = summarize(res, post["link"])
            if prev is not None:
                msg = "🔄 Змінилось!\n" + msg
            send(msg, img)
            qstate[key] = ph
            return True
        if UNCERTAIN == "send":
            why = "ключ Claude не задано" if not API_KEY else f"не знайшов чергу {QUEUE} на картинці"
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
