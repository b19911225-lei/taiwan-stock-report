#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
司法院裁判書查詢系統（judgment.judicial.gov.tw/FJUD/）批次檢索與全文下載

用途：在「可以連上司法院網站」的電腦（例如你的本機）執行，
      依關鍵字檢索裁判書、抓取全文並存成 txt，另產出一份 index.csv。

    pip install requests
    python fjud_search.py --kw "年金保單價值準備金 and 受益人" --out ./judgments

重要提醒
--------
1. 本腳本「未經實機驗證」。撰寫環境的網路出口政策封鎖司法院網站
   （gateway 回 403），無法對線上系統實際測試。
   司法院系統為 ASP.NET WebForms，欄位名稱與流程偶有調整；
   若查詢失敗，請先用 --debug 把回應 HTML 存下來，
   對照實際表單欄位名稱後調整 FIELD_HINTS。
2. 本腳本以「自動探測表單欄位」方式撰寫，對欄位改名有一定容忍度。
3. 請自行控制檢索頻率（預設每次請求間隔 1.5 秒），勿對公務系統造成負擔。
4. 若腳本仍失敗，最穩的替代方案是改用 Playwright 驅動真實瀏覽器
   （見檔案末尾說明），或直接以網頁 UI 手動檢索。
"""

import argparse
import csv
import html
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("請先安裝 requests：pip install requests")

BASE = "https://judgment.judicial.gov.tw"
FJUD = BASE + "/FJUD"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 表單欄位「名稱片段」提示：用來在探測到的所有 input 中認出該填哪一個
FIELD_HINTS = {
    "keyword": ("jud_kw", "kw"),          # 全文檢索關鍵字
    "casetype": ("vtype",),               # 裁判類別 JUDBOOK=民事…（依站方定義）
    "court": ("jud_court",),              # 法院代碼，空字串=全部
    "sys": ("jud_sys",),                  # 審判系統（民事/刑事/行政）
    "submit": ("btnQry", "btnSimpleQry"),  # 送出查詢按鈕
}

HIDDEN_KEYS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
               "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATEENCRYPTED")

INPUT_RE = re.compile(
    r"""<input\b[^>]*?name\s*=\s*["']([^"']+)["'][^>]*?>""", re.I | re.S)
VALUE_RE = re.compile(r"""value\s*=\s*["']([^"']*)["']""", re.I | re.S)
TYPE_RE = re.compile(r"""type\s*=\s*["']([^"']*)["']""", re.I | re.S)
DETAIL_RE = re.compile(
    r"""href\s*=\s*["'](/?FJUD/)?(data\.aspx\?[^"']*?ty=JD[^"']*)["']""", re.I)
TOTAL_RE = re.compile(r"共\s*([\d,]+)\s*筆")


def sleep(sec):
    time.sleep(sec)


def parse_inputs(page):
    """把頁面上所有 <input name=...> 抓成 dict，並記錄 type。"""
    fields, types = {}, {}
    for m in INPUT_RE.finditer(page):
        tag = m.group(0)
        name = html.unescape(m.group(1))
        v = VALUE_RE.search(tag)
        t = TYPE_RE.search(tag)
        fields[name] = html.unescape(v.group(1)) if v else ""
        types[name] = (t.group(1).lower() if t else "text")
    return fields, types


def find_field(fields, hints):
    """在欄位名稱中找出第一個包含任一 hint 的名稱（不分大小寫）。"""
    lowered = {n.lower(): n for n in fields}
    for hint in hints:
        for low, orig in lowered.items():
            if hint.lower() in low:
                return orig
    return None


def strip_tags(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h\d)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def dump(debug_dir, name, text):
    if not debug_dir:
        return
    os.makedirs(debug_dir, exist_ok=True)
    with open(os.path.join(debug_dir, name), "w", encoding="utf-8") as f:
        f.write(text)


def search(sess, keyword, court, casetype, debug_dir, delay):
    r = sess.get(FJUD + "/default.aspx", timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    dump(debug_dir, "01_default.html", r.text)

    fields, types = parse_inputs(r.text)
    if not any(k in fields for k in HIDDEN_KEYS):
        raise RuntimeError("首頁未取得 ASP.NET 隱藏欄位，站方頁面結構可能已變更。"
                           "請用 --debug 檢視 01_default.html。")

    # 只保留隱藏欄位與需要填的欄位，其餘 submit/checkbox 一律不送
    payload = {k: v for k, v in fields.items()
               if k in HIDDEN_KEYS or types.get(k) in ("text", "hidden")}

    kw_field = find_field(fields, FIELD_HINTS["keyword"])
    if not kw_field:
        raise RuntimeError("找不到關鍵字輸入欄位，請用 --debug 檢視表單欄位名稱後調整 FIELD_HINTS。")
    payload[kw_field] = keyword

    ct = find_field(fields, FIELD_HINTS["casetype"])
    if ct and casetype:
        payload[ct] = casetype
    cf = find_field(fields, FIELD_HINTS["court"])
    if cf is not None:
        payload[cf] = court or ""

    sub = find_field(fields, FIELD_HINTS["submit"])
    if sub:
        payload[sub] = fields.get(sub) or "送出查詢"

    sleep(delay)
    r2 = sess.post(FJUD + "/default.aspx", data=payload, timeout=60,
                   headers={"Referer": FJUD + "/default.aspx"})
    r2.raise_for_status()
    r2.encoding = "utf-8"
    dump(debug_dir, "02_result.html", r2.text)
    return r2


def collect_ids(sess, first_page, max_pages, debug_dir, delay):
    """從結果頁逐頁蒐集 data.aspx?ty=JD&id=... 連結。"""
    ids, seen = [], set()
    page_url = first_page.url
    text = first_page.text

    total = TOTAL_RE.search(text)
    if total:
        print(f"[i] 系統回報命中 {total.group(1)} 筆")

    for page_no in range(1, max_pages + 1):
        found = 0
        for m in DETAIL_RE.finditer(text):
            q = html.unescape(m.group(2))
            if q in seen:
                continue
            seen.add(q)
            ids.append(q)
            found += 1
        print(f"[i] 第 {page_no} 頁：新增 {found} 筆（累計 {len(ids)}）")
        if found == 0:
            break

        # 結果清單分頁：qryresultlst.aspx?...&page=N
        base = page_url.split("&page=")[0]
        if "qryresultlst" not in base:
            m = re.search(r"""href\s*=\s*["']([^"']*qryresultlst\.aspx[^"']*)["']""",
                          text, re.I)
            if not m:
                break
            base = BASE + "/FJUD/" + html.unescape(m.group(1)).lstrip("/").replace("FJUD/", "")
            base = base.split("&page=")[0]
        nxt = f"{base}&page={page_no + 1}"
        sleep(delay)
        rr = sess.get(nxt, timeout=60, headers={"Referer": page_url})
        if rr.status_code != 200:
            break
        rr.encoding = "utf-8"
        dump(debug_dir, f"03_list_p{page_no + 1}.html", rr.text)
        page_url, text = nxt, rr.text
    return ids


def fetch_one(sess, query, debug_dir, delay):
    url = FJUD + "/" + query.lstrip("/")
    sleep(delay)
    r = sess.get(url, timeout=60, headers={"Referer": FJUD + "/default.aspx"})
    r.raise_for_status()
    r.encoding = "utf-8"
    body = r.text

    m = re.search(r"""(?is)<div[^>]*class=["'][^"']*text-pre[^"']*["'][^>]*>(.*?)</div>""", body)
    if not m:
        m = re.search(r"""(?is)<div[^>]*id=["']jud["'][^>]*>(.*?)</div>\s*</div>""", body)
    full = strip_tags(m.group(1)) if m else strip_tags(body)

    def grab(label):
        mm = re.search(r"(?is)" + label + r"[^<]{0,10}</[^>]+>\s*<[^>]+>(.*?)</", body)
        return strip_tags(mm.group(1)) if mm else ""

    meta = {
        "url": url,
        "court": grab("裁判法院") or grab("法院"),
        "no": grab("裁判字號"),
        "date": grab("裁判日期"),
        "cause": grab("裁判案由"),
    }
    if not meta["no"]:
        mm = re.search(r"(\d{2,3}\s*年度[^\s，,]{1,12}字第\s*\d+\s*號)", full)
        if mm:
            meta["no"] = mm.group(1)
    return meta, full


def safe_name(s, fallback):
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", (s or "").strip())
    s = re.sub(r"\s+", "_", s)[:120]
    return s or fallback


def main():
    ap = argparse.ArgumentParser(description="司法院裁判書批次檢索／全文下載")
    ap.add_argument("--kw", required=True,
                    help='全文檢索字串，支援站方語法，例：'
                         '"年金保單價值準備金 and 受益人"')
    ap.add_argument("--out", default="./judgments", help="輸出目錄")
    ap.add_argument("--court", default="", help="法院代碼，留空=全部")
    ap.add_argument("--casetype", default="JUDBOOK", help="裁判類別代碼（預設 JUDBOOK）")
    ap.add_argument("--max-pages", type=int, default=10, help="最多翻幾頁清單")
    ap.add_argument("--max-docs", type=int, default=60, help="最多下載幾篇全文")
    ap.add_argument("--delay", type=float, default=1.5, help="每次請求間隔秒數")
    ap.add_argument("--debug", default="", help="除錯用：把每個回應 HTML 存到此目錄")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Accept-Language": "zh-TW,zh;q=0.9"})

    print(f"[i] 檢索字串：{args.kw}")
    res = search(sess, args.kw, args.court, args.casetype, args.debug, args.delay)
    ids = collect_ids(sess, res, args.max_pages, args.debug, args.delay)
    if not ids:
        print("[!] 沒有取得任何裁判書連結。可能是查無資料，或表單欄位已變更。\n"
              "    請加 --debug ./dbg 重跑，檢視 02_result.html 確認。")
        return 1
    ids = ids[:args.max_docs]
    print(f"[i] 準備下載 {len(ids)} 篇全文")

    rows = []
    for i, q in enumerate(ids, 1):
        try:
            meta, full = fetch_one(sess, q, args.debug, args.delay)
        except Exception as e:
            print(f"[!] 第 {i} 篇下載失敗：{e}")
            continue
        fname = safe_name(f"{meta['court']}_{meta['no']}_{meta['cause']}", f"doc{i:03d}") + ".txt"
        path = os.path.join(args.out, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {meta['court']} {meta['no']}（{meta['cause']}）\n")
            f.write(f"# 裁判日期：{meta['date']}\n# 來源：{meta['url']}\n\n")
            f.write(full)
        rows.append({**meta, "file": fname, "chars": len(full)})
        print(f"[{i}/{len(ids)}] {meta['court']} {meta['no']} → {fname}（{len(full)} 字）")

    idx = os.path.join(args.out, "index.csv")
    with open(idx, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["court", "no", "date", "cause", "chars", "file", "url"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n[✓] 完成，共 {len(rows)} 篇，清單：{idx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# 若上面的 requests 版本因站方改版而失敗，改用 Playwright 驅動真實瀏覽器：
#     pip install playwright && playwright install chromium
# 然後以 page.goto(FJUD + "/default.aspx")、page.fill(關鍵字欄位)、
# page.click(查詢按鈕)、逐頁 page.locator("a[href*='ty=JD']") 取連結，
# 再對每篇 page.goto 後 page.inner_text("div.text-pre") 取全文。
# 瀏覽器方式對 ASP.NET 的 ViewState／JS 分頁最不容易壞。
# ---------------------------------------------------------------------------
