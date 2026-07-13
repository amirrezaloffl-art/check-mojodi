#!/usr/bin/env python3
"""
Telegram stock watcher with in-chat commands, designed for GitHub Actions.

Runs automatically on a schedule (every 20 min). Each run:
  1) Applies any Telegram commands you sent since the last run.
  2) Checks every watched product and alerts ONLY on stock changes.

Commands:
  /add <url>       add a product URL
  /remove <url|N>  remove a product (by URL or by number from /list)
  /list            show the watchlist
  /check           report only what CHANGED since the last check (short)
  /report          send FULL status of every product as a .txt file
  /help            show commands

State files (bot-managed, committed back by the workflow):
  watchlist.json     -> watched URLs + Telegram update offset
  stock_state.json   -> last known per-variant availability
"""

import os, re, json, html, time, io
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
CHAT_ID   = os.environ.get("CHAT_ID",   "PUT_YOUR_CHAT_ID_HERE")

WATCHLIST_FILE = "watchlist.json"
STATE_FILE     = "stock_state.json"

REQUEST_SLEEP  = 1.0     # seconds between product fetches (be polite, stay fast)
TG_LIMIT       = 3800    # safe margin under Telegram's 4096-char message cap
MANY_CHANGES   = 15      # more changes than this -> send as a file, not a message

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
def _post(msg: str):
    try:
        r = requests.post(f"{API}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=30)
        if not r.ok:
            print("sendMessage failed:", r.status_code, r.text[:200])
    except Exception as e:
        print("send error:", e)

def send(msg: str):
    """Send a message, splitting it if it exceeds Telegram's length limit."""
    if "PUT_YOUR" in BOT_TOKEN or "PUT_YOUR" in CHAT_ID:
        print("[!] Telegram not configured. Would send:\n" + msg)
        return
    if len(msg) <= TG_LIMIT:
        _post(msg)
        return
    # split on blank lines first, then hard-split any oversized piece
    chunk, out = "", []
    for block in msg.split("\n\n"):
        while len(block) > TG_LIMIT:
            out.append(block[:TG_LIMIT]); block = block[TG_LIMIT:]
        if len(chunk) + len(block) + 2 > TG_LIMIT:
            out.append(chunk); chunk = block
        else:
            chunk = (chunk + "\n\n" + block) if chunk else block
    if chunk:
        out.append(chunk)
    for part in out:
        _post(part)
        time.sleep(0.4)          # stay under Telegram's rate limit

def send_document(filename: str, content: str, caption: str = ""):
    """Send text as a file attachment — bypasses the message length limit."""
    if "PUT_YOUR" in BOT_TOKEN or "PUT_YOUR" in CHAT_ID:
        print(f"[!] Telegram not configured. Would send file {filename}:\n{content[:500]}")
        return
    try:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:1000]},
            files={"document": (filename, io.BytesIO(content.encode("utf-8")),
                                "text/plain")},
            timeout=60)
        if not r.ok:
            print("sendDocument failed:", r.status_code, r.text[:200])
    except Exception as e:
        print("send_document error:", e)

def get_updates(offset: int):
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
    """Apply pending commands. Returns set of requested modes: {'check','report'}."""
    updates = get_updates(watch.get("offset", 0))
    modes = set()
    urls = watch.setdefault("urls", [])

    for u in updates:
        watch["offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue                                  # allowlist: only you
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "add":
            # allow several links in one message, one per line or space-separated
            found = URL_RE.findall(arg)
            if not found:
                send("❌ لینک نامعتبر. مثال:\n/add https://site.com/product/x/")
            else:
                added = 0
                for link in found:
                    if link not in urls:
                        urls.append(link); added += 1
                send(f"✅ {added} لینک اضافه شد. مجموع: {len(urls)} مورد.")

        elif cmd in ("remove", "rm", "del"):
            target = None
            if arg.isdigit():
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
            elif len(urls) > 40:
                # too many to read comfortably in chat -> send as a file
                body = "\n".join(f"{i+1}. {u}" for i, u in enumerate(urls))
                send_document("watchlist.txt", body,
                              caption=f"📋 لیست فعلی: {len(urls)} مورد")
            else:
                lines = [f"{i+1}. {html.escape(u)}" for i, u in enumerate(urls)]
                send("<b>لیست فعلی:</b>\n" + "\n".join(lines))

        elif cmd in ("check", "now"):
            modes.add("check")

        elif cmd in ("report", "full", "status"):
            modes.add("report")
            send("📄 در حال ساخت گزارش کامل…")

        elif cmd in ("help", "start"):
            send("<b>دستورها:</b>\n"
                 "/add &lt;لینک&gt; — افزودن محصول (چند لینک هم می‌شود)\n"
                 "/remove &lt;لینک یا شماره&gt; — حذف\n"
                 "/list — نمایش لیست\n"
                 "/check — فقط تغییرات نسبت به بررسی قبلی\n"
                 "/report — وضعیت کامل همه، به صورت فایل متنی\n"
                 "/help — همین راهنما\n\n"
                 "ℹ️ بررسی خودکار هر ۲۰ دقیقه انجام می‌شود و "
                 "تغییر موجودی خودکار اطلاع داده می‌شود.")
        else:
            send("دستور ناشناخته. /help را بزن.")

    return modes


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


def run_checks(urls, state, want_report=False, announce_changes=True):
    """Fetch all products. Alert on changes. Optionally build a full report file."""
    new_state  = {}
    titles     = {}
    changes    = []     # (title, label, in_stock, url)
    errors     = []

    for url in urls:
        try:
            title, variants = check(url)
        except Exception as e:
            print(f"[error] {url}: {e}")
            new_state[url] = state.get(url, {})       # preserve old state on failure
            errors.append(url)
            continue

        titles[url]    = title
        old            = state.get(url, {})
        new_state[url] = variants

        for label, in_stock in variants.items():
            prev = old.get(label)
            if prev is not None and prev != in_stock:
                changes.append((title, label, in_stock, url))

        print(f"[ok] {title}: " +
              ", ".join(f"{k}={'IN' if v else 'OUT'}" for k, v in variants.items()))
        time.sleep(REQUEST_SLEEP)

    # --- notify only about changes, grouped per product ---
    if announce_changes and changes:
        # group: one block per product, listing all its changed variants
        by_product = {}
        for title, label, in_stock, url in changes:
            by_product.setdefault(url, {"title": title, "items": []})
            by_product[url]["items"].append((label, in_stock))

        n_changes  = len(changes)
        n_products = len(by_product)

        if n_changes <= MANY_CHANGES:
            # few changes -> readable message(s); send() auto-splits if needed
            blocks = []
            for url, info in by_product.items():
                lines = [f"<b>{html.escape(info['title'])}</b>"]
                for label, in_stock in info["items"]:
                    status = "✅ موجود شد" if in_stock else "❌ ناموجود شد"
                    lines.append(f"{html.escape(label)}: {status}")
                lines.append(url)
                blocks.append("\n".join(lines))
            send(f"🔔 <b>{n_changes} تغییر موجودی</b> "
                 f"({n_products} محصول)\n\n" + "\n\n".join(blocks))
        else:
            # many changes -> short summary + details as a file (no length limit)
            n_in  = sum(1 for _, _, s, _ in changes if s)
            n_out = n_changes - n_in
            buf = [f"تغییرات موجودی — {n_changes} تغییر در {n_products} محصول",
                   time.strftime("%Y-%m-%d %H:%M UTC"), "=" * 50, ""]
            for url, info in by_product.items():
                buf.append(info["title"])
                for label, in_stock in info["items"]:
                    buf.append(f"   {'✅ موجود شد  ' if in_stock else '❌ ناموجود شد'}  {label}")
                buf.append(f"   {url}")
                buf.append("")
            send_document(
                "changes.txt", "\n".join(buf),
                caption=(f"🔔 {n_changes} تغییر موجودی در {n_products} محصول\n"
                         f"✅ موجود شد: {n_in}   |   ❌ ناموجود شد: {n_out}"))

    # --- full report as a file (only on /report) ---
    if want_report:
        buf = [f"گزارش کامل موجودی — {len(urls)} محصول",
               time.strftime("%Y-%m-%d %H:%M UTC"), "=" * 50, ""]
        for url in urls:
            t = titles.get(url, url)
            buf.append(t)
            vs = new_state.get(url, {})
            if not vs:
                buf.append("   ⚠️ خطا در دریافت")
            for label, in_stock in vs.items():
                buf.append(f"   {'موجود   ✅' if in_stock else 'ناموجود ❌'}  {label}")
            buf.append(f"   {url}")
            buf.append("")
        if errors:
            buf += ["", f"⚠️ {len(errors)} مورد خطا داشت:"] + [f"   {u}" for u in errors]
        send_document("stock_report.txt", "\n".join(buf),
                      caption=f"📄 وضعیت کامل: {len(urls)} محصول"
                              f"{f' | {len(changes)} تغییر' if changes else ''}")

    return new_state, changes, errors


def main():
    watch = load_json(WATCHLIST_FILE, {"urls": [], "offset": 0})
    state = load_json(STATE_FILE, {})

    modes = handle_commands(watch)
    save_json(WATCHLIST_FILE, watch)          # persist watchlist + offset first

    urls = watch.get("urls", [])
    new_state, changes, errors = run_checks(
        urls, state, want_report=("report" in modes))
    save_json(STATE_FILE, new_state)

    # /check asked for an explicit answer -> confirm even if nothing changed
    if "check" in modes and not changes:
        msg = f"✅ بررسی شد: {len(urls)} محصول — هیچ تغییری نبود."
        if errors:
            msg += f"\n⚠️ {len(errors)} مورد خطا داشت."
        send(msg)

    print(f"done: {len(urls)} urls, {len(changes)} changes, {len(errors)} errors")


if __name__ == "__main__":
    main()
