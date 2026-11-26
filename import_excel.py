#!/usr/bin/env python3
"""
"Список клиенти — Ходимларга" Excel faylini bot bazasiga bir marta ko'chirish (faqat standart kutubxona).

  python3 import_excel.py FAYL.xlsx            # nima import qilinishini ko'rsatadi (bazaga yozmaydi)
  python3 import_excel.py FAYL.xlsx --apply    # bazaga yozadi

Nima ko'chiriladi:
  * har oylik varaq ("Февраль 2026" ...) -> filings jadvali (korxona × soliq turi × davr), manba = "excel".
    soliq.uz dan kelgan yoki qo'lda belgilangan kataklar ustidan yozilmaydi.
  * oxirgi varaq -> korxonalar ro'yxati (nom, faoliyat turi, Excel'dagi hodim, 1C).
  * oxirgi 12 oy -> har korxona qaysi soliqlarni topshiradi (obligations) va qay chastotada (oylik / choraklik).
"""
import json
import os
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
RU_MONTHS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("май", 5), ("мая", 5), ("июн", 6),
             ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12)]
# Excel ustun sarlavhasi (kichik harf) -> kod. Tartib muhim: birinchi mos kelgani olinadi.
HEADERS = [("подоход", "jshds"), ("инпс", "inps"), ("ндс", "nds"), ("дивиденд", "div"), ("енп", "enp"),
           ("прибыл", "prib"), ("имущ", "imush"), ("земл", "zem"), ("вода", "voda"), ("акциз", "akciz"),
           ("баланс", "balans"), ("фин", "finres"), ("статист", "stat"),
           ("ходим", "staff"), ("1c", "c1"), ("1с", "c1"), ("инн", "inn")]
TAX_CODES = {"jshds", "inps", "nds", "div", "enp", "prib", "imush", "zem", "voda", "akciz", "balans", "finres", "stat"}


def col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n


def read_xlsx(path):
    """-> [(varaq nomi, {qator: {ustun: matn}})] Excel'dagi tartibda."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = []
    for sh in wb.find("m:sheets", NS):
        rid = sh.get("{%s}id" % NS["r"])
        t = target[rid].lstrip("/")
        t = t if t.startswith("xl/") else "xl/" + t
        rows = {}
        for c in ET.fromstring(z.read(t)).iter("{%s}c" % NS["m"]):
            ref, typ = c.get("r"), c.get("t")
            v = c.find("m:v", NS)
            if typ == "s" and v is not None:
                val = shared[int(v.text)]
            elif typ == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
            else:
                val = v.text if v is not None else ""
            val = " ".join(str(val or "").split())
            if val.endswith(".0") and val[:-2].isdigit():
                val = val[:-2]
            if val:
                r = int(re.search(r"\d+", ref).group(0))
                rows.setdefault(r, {})[col_index(ref)] = val
        out.append((sh.get("name"), rows))
    return out


def sheet_period(name):
    """'Февраль 2026' -> '2026-02'. Nusxa varaqlar ('Май 2024 (2)') va oy nomisizlar -> None."""
    s = name.lower().strip()
    if "(" in s:
        return None
    y = re.search(r"(20\d\d)", s)
    m = next((n for k, n in RU_MONTHS if k in s), None)
    return f"{y.group(1)}-{m:02d}" if y and m else None


def cell_status(v):
    s = (v or "").lower().replace("ў", "у").replace("ё", "е")
    if not s:
        return None
    if "топшир" in s or "сдан" in s:
        return "ok"
    if s in ("йук", "йок", "нет", "-", "—", "х", "x") or "шарт эмас" in s:
        return "na"
    return "note"


def parse(path):
    sheets = []
    for name, rows in read_xlsx(path):
        per = sheet_period(name)
        if not per:
            sheets.append((name, None, []))
            continue
        hdr_row = next((r for r in sorted(rows) if any("инн" == v.lower() for v in rows[r].values())), None)
        if hdr_row is None:
            sheets.append((name, None, []))
            continue
        cols = {}
        for c, v in rows[hdr_row].items():
            lv = v.lower()
            code = next((k for h, k in HEADERS if h in lv), None)
            if code and code not in cols.values():
                cols[c] = code
        inn_col = next(c for c, k in cols.items() if k == "inn")
        items = []
        for r in sorted(rows):
            if r <= hdr_row:
                continue
            row = rows[r]
            name_ = row.get(2, "").strip()
            inn = re.sub(r"\D", "", row.get(inn_col, ""))
            if not name_ or name_.lower().startswith("наименование"):
                continue
            it = {"row": r, "name": name_, "inn": inn if len(inn) in (9, 14) else "", "raw_inn": row.get(inn_col, ""),
                  "activity": row.get(inn_col - 1, "") if inn_col - 1 > 2 else "", "staff": "", "c1": "", "taxes": {}}
            for c, k in cols.items():
                v = row.get(c, "")
                if k == "staff":
                    it["staff"] = v
                elif k == "c1":
                    it["c1"] = v
                elif k in TAX_CODES:
                    st = cell_status(v)
                    if st:
                        it["taxes"][k] = (st, v)
            items.append(it)
        sheets.append((name, per, items))
    return sheets


def obligations(sheets, last_n=12):
    """Oxirgi `last_n` oy bo'yicha: {(inn, tax): freq}. freq: 'm' oylik, 'q' choraklik (3,6,9,12-oylar)."""
    dated = sorted([s for s in sheets if s[1]], key=lambda s: s[1])
    seen_periods = sorted({s[1] for s in dated})[-last_n:]
    months = {}
    for name, per, items in dated:
        if per not in seen_periods:
            continue
        for it in items:
            if not it["inn"]:
                continue
            for k, (st, _v) in it["taxes"].items():
                if st in ("ok", "note"):
                    months.setdefault((it["inn"], k), set()).add(int(per[5:7]))
    return {key: ("m" if any(m % 3 for m in ms) else "q") for key, ms in months.items()}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path, apply = sys.argv[1], "--apply" in sys.argv
    sheets = parse(path)
    dated = sorted([s for s in sheets if s[1]], key=lambda s: s[1])
    for name, per, items in sheets:
        print(f"  {name!r:24} -> {per or 'O‘TKAZIB YUBORILDI'}  ({len(items)} korxona)")
    if not dated:
        sys.exit("Oylik varaq topilmadi.")
    last_name, last_per, last_items = dated[-1]
    print(f"\nKorxonalar ro'yxati oxirgi varaqdan olinadi: {last_name} ({len(last_items)} qator)")
    no_inn = [it for it in last_items if not it["inn"]]
    if no_inn:
        print("⚠️  INN'siz qatorlar (import qilinmaydi, dashboardda qo'lda qo'shing):")
        for it in no_inn:
            print(f"     qator {it['row']}: {it['name']}  ИНН={it['raw_inn']!r}")
    obl = obligations(sheets)
    from collections import Counter
    print("\nSoliq turlari bo'yicha majburiyatlar (korxona soni, oylik/choraklik):")
    cnt = Counter((k, f) for (_i, k), f in obl.items())
    for k in sorted({k for k, _ in cnt}):
        print(f"   {k:8} oylik={cnt.get((k, 'm'), 0):3}  choraklik={cnt.get((k, 'q'), 0):3}")

    if not apply:
        print("\n(Bazaga yozilmadi. Yozish uchun: --apply)")
        return

    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    db = sqlite3.connect(cfg.get("DB_PATH", os.path.join(base, "data.db")))
    have = {r[1] for r in db.execute("PRAGMA table_info(companies)")}
    if "activity" not in have or not db.execute("SELECT 1 FROM sqlite_master WHERE name='filings'").fetchone():
        sys.exit("Bazada yangi jadvallar yo'q — avval yangi bot.py ni bir marta ishga tushiring (systemctl restart hodim-bot).")
    ts = datetime.now().isoformat(timespec="seconds")
    n_f = 0
    for name, per, items in dated:
        for it in items:
            if not it["inn"]:
                continue
            for k, (st, v) in it["taxes"].items():
                cur = db.execute("UPDATE filings SET status=?, detail=?, ts=? WHERE inn=? AND period=? AND tax=? "
                                 "AND source='excel'", (st, v, ts, it["inn"], per, k))
                if cur.rowcount == 0:
                    cur = db.execute("INSERT OR IGNORE INTO filings(inn,period,tax,status,source,report_id,detail,ts) "
                                     "VALUES(?,?,?,?,'excel',NULL,?,?)", (it["inn"], per, k, st, v, ts))
                n_f += cur.rowcount
    n_c = 0
    for i, it in enumerate(last_items):
        if not it["inn"]:
            continue
        db.execute("INSERT INTO companies(inn,name,tg_id) VALUES(?,?,NULL) ON CONFLICT(inn) DO NOTHING",
                   (it["inn"], it["name"]))
        db.execute("UPDATE companies SET name=COALESCE(NULLIF(name,''),?), activity=?, staff=?, c1=?, sort=?, active=1 "
                   "WHERE inn=?", (it["name"], it["activity"], it["staff"], it["c1"], i + 1, it["inn"]))
        n_c += 1
    # Majburiyatlar: Excel'dan hisoblangani; dashboardda qo'lda o'zgartirilganlari (source='manual') saqlanadi.
    # Oxirgi varaqda "йук" bo'lgan (va oxirgi 12 oyda topshirilmagan) soliqlar — "topshirmaydi" deb belgilanadi,
    # jadvalda Excel'dagidek "йук" ko'rinadi.
    for it in last_items:
        for k, (st, _v) in it["taxes"].items():
            if it["inn"] and st == "na" and (it["inn"], k) not in obl:
                db.execute("INSERT INTO obligations(inn,tax,required,freq,source) VALUES(?,?,0,'m','excel') "
                           "ON CONFLICT(inn,tax) DO UPDATE SET required=0 WHERE obligations.source='excel'", (it["inn"], k))
    for (inn, k), fr in obl.items():
        db.execute("INSERT INTO obligations(inn,tax,required,freq,source) VALUES(?,?,1,?,'excel') "
                   "ON CONFLICT(inn,tax) DO UPDATE SET required=1, freq=excluded.freq WHERE obligations.source='excel'",
                   (inn, k, fr))
    # Shu oygacha bo'lgan davrlar "tarix" — jadval faqat Excel'dagi holatni ko'rsatadi (taxminiy "qarz" chiqarmaydi).
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('excel_last_period',?)", (last_per,))
    db.commit()
    print(f"\n✅ Yozildi: {n_c} korxona, {n_f} katak, {len(obl)} majburiyat.")


if __name__ == "__main__":
    main()
