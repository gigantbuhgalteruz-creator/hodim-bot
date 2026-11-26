#!/usr/bin/env python3
"""
Cloudflare quick tunnel: botni (localhost:8080) internetga https manzil bilan chiqaradi.
Har ishga tushganda manzil o'zgaradi, shuning uchun yangi manzil config.json ga yoziladi
va rahbarga Telegram orqali yuboriladi.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
_local = os.path.join(BASE, "cloudflared", "cloudflared.exe")          # Windows: papkadagi exe
EXE = _local if os.path.exists(_local) else (__import__("shutil").which("cloudflared") or "cloudflared")  # Linux
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_url(url):
    cfg = load_cfg()
    cfg["PUBLIC_URL"] = url
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)
    return cfg


def publish_url(cfg, url):
    """Bot profilidagi qisqa tavsif — server manzili endi YOZILMAYDI (hodimlar ko'rmasin).
    Agent va skript yangi manzilni serverning doimiy IP manzilidagi /u dan (imzolangan) oladi."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg['BOT_TOKEN']}/setMyShortDescription",
        data=json.dumps({"short_description": "Hodimlar davomati va hisobotlar."}).encode(),
        headers={"Content-Type": "application/json"})
    for _ in range(5):
        try:
            urllib.request.urlopen(req, timeout=30).read()
            print("Bot profiliga manzil yozildi")
            return
        except Exception as e:
            print("Bot profilini yangilab bo'lmadi:", e)
            time.sleep(10)


def notify(cfg, url):
    text = (f"🌐 <b>Server manzili yangilandi</b>\n{url}\n\n"
            f"📈 Dashboard: {url}/dashboard\n\n"
            f"Hodimlar skripti (v3.6+) va kompyuter agenti yangi manzilni o'zi topadi — hech narsa qilish shart emas.")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg['BOT_TOKEN']}/sendMessage",
        data=json.dumps({"chat_id": int(cfg["OWNER_ID"]), "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"})
    for _ in range(5):
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except Exception as e:
            print("Telegramga yuborib bo'lmadi:", e)
            time.sleep(10)


def run_once():
    port = load_cfg().get("PORT", 8080)
    p = subprocess.Popen([EXE, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         encoding="utf-8", errors="replace")
    sent = False
    for line in p.stdout:
        print(line, end="")
        m = URL_RE.search(line)
        if m and not sent:
            url = m.group(0)
            print(f"\n==> Yangi manzil: {url}\n")
            cfg = save_url(url)
            publish_url(cfg, url)
            notify(cfg, url)
            sent = True
    p.wait()


if __name__ == "__main__":
    while True:
        try:
            run_once()
        except Exception as e:
            print("Xato:", e)
        print("Tunnel to'xtadi, 15 soniyadan keyin qayta ishga tushadi...")
        time.sleep(15)
