#!/usr/bin/env python3
"""每天跟著 KKday 公開網頁更新 japan-rail-finder.html 裡的價格與發車時間。

更新欄位：起價 p、最早/最晚發車 a/z、行車時間 d。
資料來源：https://www.kkday.com/zh-tw/transportation/japan-rail/<頁面>
  - TC    觀光/特急列車   → <s>
  - CARDS 新幹線/路線卡    → <u>
  - LTD   長尾路線         → route/<slug>
抓不到、或新值看起來不合理時，保留舊值並在報告中列出，不會亂改。
用法：python3 update_prices.py [html 路徑]
"""
import json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

PATH = sys.argv[1] if len(sys.argv) > 1 else "japan-rail-finder.html"
BASE = "https://www.kkday.com/zh-tw/transportation/japan-rail/"
WORKERS = 6          # 同時請求數（不要太高，避免對 KKday 造成負擔）
# 長尾路線（LTD）分成 SLICES 份，每天輪流更新其中一份（預設 7 份 = 每條路線每週更新一次）。
# 主要商品卡（TC、CARDS）每天都全部更新。設 SLICES=1 可一次更新全部。
SLICES = max(1, int(os.environ.get("SLICES", "7")))
import datetime
TODAY_SLICE = datetime.date.today().toordinal() % SLICES
MAX_JUMP = 0.5       # 新價格與舊價格差超過 50% 視為異常，不採用
MAX_FAIL_RATE = 0.2  # 失敗比例超過 20% 時整個流程標示失敗（不寫檔）

html = open(PATH, encoding="utf-8").read()


def fetch(path):
    for i in range(2):
        r = subprocess.run(
            ["curl", "-s", "-L", "-A", "Mozilla/5.0", "--compressed", "-m", "25",
             "-w", "\n%{http_code} %{url_effective}", BASE + path],
            capture_output=True)
        out = r.stdout.decode("utf-8", "replace")
        body, _, tail = out.rpartition("\n")
        code, _, final = tail.partition(" ")
        # 被導回總覽頁 = 這個頁面已不存在
        if code == "200" and len(body) > 20000 and final.rstrip("/").endswith("/japan-rail") is False:
            return body
        time.sleep(1.5 * (i + 1))
    return None


def parse(h):
    g = lambda p: (re.search(p, h) or [None, None])[1]
    lp = g(r'"lowest_price":(\d+)')
    return dict(
        p=int(lp) if lp else None,
        a=g(r'"earliest_dep_time":"(\d{4})"'),
        z=g(r'"latest_dep_time":"(\d{4})"'),
        d=g(r'"lowest_price_route":\{.*?"duration":"(\d{4})"'),
    )


def hm(d):  # "0130" -> "1時30分"
    return f"{int(d[:2])}時{d[2:]}分"


def mins(d):
    return int(d[:2]) * 60 + int(d[2:])


def jump_ok(old, new):
    return old and new and abs(new - old) / old <= MAX_JUMP


# ---------- 讀取三份資料 ----------
def load_json_var(name):
    m = re.search(r"var " + name + r"=(\[.*?\]);\n", html, re.S)
    return m, json.loads(m.group(1))


m_tc, TC = load_json_var("TC")
m_cards, CARDS = load_json_var("CARDS")
m_ltd = re.search(r"var LTD=(\[\[.*?\]\]);", html, re.S)
LTD = json.loads(m_ltd.group(1))

jobs = [("TC", i, c["s"]) for i, c in enumerate(TC)]
jobs += [("CARDS", i, c["u"]) for i, c in enumerate(CARDS)]
jobs += [("LTD", i, "route/" + r[6]) for i, r in enumerate(LTD) if i % SLICES == TODAY_SLICE]
print(f"共 {len(jobs)} 個頁面要檢查（長尾路線今天更新第 {TODAY_SLICE + 1}/{SLICES} 份）", flush=True)


def run(j):
    kind, i, path = j
    h = fetch(path)
    return kind, i, path, (parse(h) if h else None)


results = []
with ThreadPoolExecutor(WORKERS) as ex:
    for n, r in enumerate(ex.map(run, jobs), 1):
        results.append(r)
        if n % 25 == 0:
            print(f"  已完成 {n}/{len(jobs)}", flush=True)

failed = [r for r in results if r[3] is None or r[3]["p"] is None]
if len(failed) / len(jobs) > MAX_FAIL_RATE:
    print(f"::error::{len(failed)}/{len(jobs)} 個頁面抓取失敗，超過門檻，這次不更新")
    sys.exit(1)

changes, skipped = [], []
for kind, i, path, r in results:
    if r is None or r["p"] is None:
        continue
    if kind == "LTD":
        row = LTD[i]
        label = f"{row[0]}–{row[1]}"
        old = dict(p=row[2], d=row[3], a=row[4], z=row[5])
        if not jump_ok(old["p"], r["p"]):
            skipped.append(f"{label}: {old['p']} → {r['p']}（差異過大，略過）")
            continue
        new = dict(p=r["p"], d=mins(r["d"]) if r["d"] else old["d"],
                   a=r["a"] or old["a"], z=r["z"] or old["z"])
        if new != old:
            row[2], row[3], row[4], row[5] = new["p"], new["d"], new["a"], new["z"]
            if new["p"] != old["p"]:
                changes.append(f"{label}: NT${old['p']} → NT${new['p']}")
    else:
        c = (TC if kind == "TC" else CARDS)[i]
        label = c.get("n") or f"{c.get('f')}–{c.get('to')}"
        old = dict(p=c.get("p"), d=c.get("d"), a=c.get("a"), z=c.get("z"))
        if not jump_ok(old["p"], r["p"]):
            skipped.append(f"{label}: {old['p']} → {r['p']}（差異過大，略過）")
            continue
        new = dict(p=r["p"], d=hm(r["d"]) if r["d"] else old["d"],
                   a=r["a"] or old["a"], z=r["z"] or old["z"])
        if new != old:
            c.update(new)
            if new["p"] != old["p"]:
                changes.append(f"{label}: NT${old['p']} → NT${new['p']}")


# ---------- 寫回（只改資料區，其餘一字不動） ----------
dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
out = html
out = out.replace(m_ltd.group(0), "var LTD=" + dump(LTD) + ";", 1)
out = out.replace(m_cards.group(0), "var CARDS=" + dump(CARDS) + ";\n", 1)
out = out.replace(m_tc.group(0), "var TC=" + dump(TC) + ";\n", 1)

changed = out != html
if changed:
    open(PATH, "w", encoding="utf-8").write(out)

lines = [f"## 價格更新報告",
         f"- 檢查頁面：{len(jobs)}，抓取失敗：{len(failed)}",
         f"- 價格有變動：{len(changes)} 筆；{'已更新檔案' if changed else '沒有需要更新的內容'}"]
if changes:
    lines += ["", "### 價格變動"] + [f"- {x}" for x in changes[:60]]
    if len(changes) > 60:
        lines.append(f"- …另外還有 {len(changes) - 60} 筆")
if skipped:
    lines += ["", "### 差異過大，已略過（請人工確認）"] + [f"- {x}" for x in skipped]
if failed:
    lines += ["", "### 抓取失敗，沿用舊資料"] + [f"- {r[2]}" for r in failed[:40]]
report = "\n".join(lines)
print(report)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(report + "\n")
if os.environ.get("GITHUB_OUTPUT"):
    open(os.environ["GITHUB_OUTPUT"], "a").write(f"changed={'true' if changed else 'false'}\nprice_changes={len(changes)}\n")
