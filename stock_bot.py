#!/usr/bin/env python3
"""
Telegram stock watcher with in-chat commands, designed for GitHub Actions.

Each scheduled run does two phases:
  1) Drain Telegram commands you sent since last run and update the watchlist:
        /add <url>       add a product URL
        /remove <url>    remove a product URL (or /remove <number> from /list)
        /list            show current watchlist
        /check           force a stock check now and report every item's status
        /help            show commands
  2) Check every product on the watchlist and alert on stock changes.

State lives in two committed JSON files (the bot manages both):
  watchlist.json     -> the URLs you're watching + Telegram update offset
  stock_state.json   -> last known per-variant availability

Setup:
  - Secrets BOT_TOKEN and CHAT_ID provided via env (GitHub Actions secrets).
  - CHAT_ID also acts as an allowlist: only that chat can command the bot.
"""

import os, re, json, html, time
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
CHAT_ID   = os.environ.get("CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

WATCHLIST_FILE = "watchlist.json"
STATE_FILE     = "stock_state.json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------------- persistence ----------------
def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default

def save_json(path, obj):
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- telegram ----------------
def send(msg: str):
    if "PUT_YOUR" in BOT_TOKEN or "PUT_YOUR" in CHAT_ID:
        print("[!] Telegram not configured. Would send:\n" + msg)
        return
    try:
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=30)
    except Exception as e:
        print("send error:", e)

def get_updates(offset: int):
    """Fetch pending commands. offset acknowledges everything before it."""
    if "PUT_YOUR" in BOT_TOKEN:
        return []
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=30)
        return r.json().get("result", [])
    except Exception as e:
        print("getUpdates error:", e)
        return []


# ---------------- command handling ----------------
URL_RE = re.compile(r"https?://\S+")

def valid_url(u: str) -> bool:
    return bool(URL_RE.fullmatch(u.strip()))

def handle_commands(watch: dict):
    """Drain and apply commands. Returns True if a forced /check was requested."""
    updates = get_updates(watch.get("offset", 0))
    force_check = False
    urls = watch.setdefault("urls", [])

    for u in updates:
        watch["offset"] = u["update_id"] + 1          # advance offset regardless
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        # allowlist: ignore anyone who isn't you
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/")
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("add",):
            if not valid_url(arg):
                send("❌ لینک نامعتبر. مثال:\n/add https://site.com/product/x/")
            elif arg in urls:
                send("ℹ️ این لینک قبلاً در لیست هست.")
            else:
                urls.append(arg)
                send(f"✅ اضافه شد ({len(urls)} مورد در لیست):\n{arg}")

        elif cmd in ("remove", "rm", "del"):
            target = None
            if arg.isdigit():                          # remove by list number
                i = int(arg) - 1
                if 0 <= i < len(urls):
                    target = urls[i]
            elif arg in urls:
                target = arg
            if target:
                urls.remove(target)
                send(f"🗑 حذف شد ({len(urls)} مورد باقی مانده):\n{target}")
            else:
                send("❌ پیدا نشد. با /list شماره یا لینک درست را ببین.")

        elif cmd in ("list", "ls"):
            if not urls:
                send("لیست خالی است. با /add یک لینک اضافه کن.")
            else:
                lines = [f"{i+1}. {html.escape(u)}" for i, u in enumerate(urls)]
                send("<b>لیست فعلی:</b>\n" + "\n".join(lines))

        elif cmd in ("check", "now"):
            force_check = True
            send("🔄 در حال بررسی همه موارد…")

        elif cmd in ("help", "start"):
            send("<b>دستورها:</b>\n"
                 "/add &lt;لینک&gt; — افزودن محصول\n"
                 "/remove &lt;لینک یا شماره&gt; — حذف\n"
                 "/list — نمایش لیست\n"
                 "/check — بررسی فوری همه\n"
                 "/help — همین راهنما")
        else:
            send("دستور ناشناخته. /help را بزن.")

    return force_check


# ---------------- stock checking ----------------
def variant_label(v: dict) -> str:
    attrs = v.get("attributes", {}) or {}
    parts = [str(x) for x in attrs.values() if x]
    return " / ".join(parts) if parts else f"variant {v.get('variation_id','?')}"

def parse_variations(soup):
    form = soup.select_one("form.variations_form")
    if not form or not form.has_attr("data-product_variations"):
        return None
    raw = html.unescape(form["data-product_variations"])
    if raw.strip() in ("false", ""):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    out = {variant_label(v): bool(v.get("is_in_stock", False)) for v in data}
    return out or None

def parse_fallback(soup, text):
    out_markers = ["ناموجود", "اتمام موجودی", "تمام شد", "out of stock"]
    in_markers  = ["افزودن به سبد", "add to cart", "موجود"]
    has_cart = bool(soup.select_one("button.single_add_to_cart_button:not(.disabled)"))
    low = text.lower()
    if any(m in text or m in low for m in out_markers) and not has_cart:
        avail = False
    elif has_cart or any(m in text or m in low for m in in_markers):
        avail = True
    else:
        avail = has_cart
    return {"(whole product)": avail}

def check(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    t = soup.select_one("h1.product_title, h1.entry-title, title")
    title = t.get_text(strip=True) if t else url
    variants = parse_variations(soup) or parse_fallback(soup, r.text)
    return title, variants


def run_checks(urls, state, report_all=False):
    new_state = {}
    report_lines = []
    for url in urls:
        try:
            title, variants = check(url)
        except Exception as e:
            print(f"[error] {url}: {e}")
            new_state[url] = state.get(url, {})
            if report_all:
                report_lines.append(f"⚠️ خطا در بررسی:\n{url}")
            continue

        old = state.get(url, {})
        new_state[url] = variants
        first_time = url not in state

        for label, in_stock in variants.items():
            prev = old.get(label)
            if prev is not None and prev != in_stock:
                status = "✅ موجود شد" if in_stock else "❌ ناموجود شد"
                send(f"<b>{html.escape(title)}</b>\n{html.escape(label)}: {status}\n{url}")
                print(f"NOTIFY {title} | {label} -> {'IN' if in_stock else 'OUT'}")

        if report_all:
            vs = "\n".join(f"  • {html.escape(k)}: "
                           f"{'✅ موجود' if v else '❌ ناموجود'}"
                           for k, v in variants.items())
            report_lines.append(f"<b>{html.escape(title)}</b>\n{vs}")
        print(f"[ok] {title}: " +
              ", ".join(f"{k}={'IN' if v else 'OUT'}" for k, v in variants.items()))
        time.sleep(2)

    if report_all:
        send("\n\n".join(report_lines) if report_lines else "لیست خالی است.")
    return new_state


def main():
    watch = load_json(WATCHLIST_FILE, {"urls": [], "offset": 0})
    state = load_json(STATE_FILE, {})

    force = handle_commands(watch)
    save_json(WATCHLIST_FILE, watch)      # persist watchlist + offset immediately

    new_state = run_checks(watch.get("urls", []), state, report_all=force)
    save_json(STATE_FILE, new_state)


if __name__ == "__main__":
    main()
