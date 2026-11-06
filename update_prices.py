#!/usr/bin/env python3
"""跟著 KKday 公開網頁更新 prices.json（japan-rail-finder.html 載入時會讀它）。

更新欄位：起價 p、行車時間 d、最早/最晚發車 a/z。
資料來源：https://www.kkday.com/zh-tw/transportation/japan-rail/<頁面路徑>
  - 觀光/特急列車 → <車名代碼>
  - 新幹線/路線卡 → shinkansen/... 或 route/...
  - 長尾路線     → route/<slug>
html 只用來列出「有哪些頁面要檢查」，這支腳本不會修改 html。

抓不到、或新價格與舊價格差超過 50% 時，保留舊值並在報告中列出。
失敗比例太高時整個不寫檔、以非 0 狀態結束。

用法：
  python3 update_prices.py              # 抓全部，更新 prices.json
  python3 update_prices.py --init       # 不上網，用 html 內嵌的值建立 prices.json
  python3 update_prices.py --dry-run    # 只列出變動，不寫檔
環境變數：SLICES=7 時，長尾路線每天只更新 1/7（輪流）；預設 1 = 全部。
"""
import datetime, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "japan-rail-finder.html")
OUT = os.path.join(HERE, "prices.json")
BASE = "https://www.kkday.com/zh-tw/transportation/japan-rail/"
WORKERS = 6
MAX_JUMP = 0.5
MAX_FAIL_RATE = 0.2
SLICES = max(1, int(os.environ.get("SLICES", "1")))
INIT = "--init" in sys.argv
DRY = "--dry-run" in sys.argv


def hm(minutes):
    return f"{minutes // 60}時{minutes % 60:02d}分"


def hm4(d):  # "0130" -> "1時30分"
    return f"{int(d[:2])}時{d[2:]}分"


# ---------- 從 html 取得所有項目與內嵌的值 ----------
html = open(HTML, encoding="utf-8").read()
tc = json.loads(re.search(r"var TC=(\[.*?\]);\n", html, re.S).group(1))
cards = json.loads(re.search(r"var CARDS=(\[.*?\]);\n", html, re.S).group(1))
ltd = json.loads(re.search(r"var LTD=(\[\[.*?\]\]);", html, re.S).group(1))

embedded = {}  # path -> {p,d,a,z}
labels = {}
for c in tc:
    embedded[c["s"]] = dict(p=c["p"], d=c["d"], a=c["a"], z=c["z"])
    labels[c["s"]] = c.get("n") or c["s"]
for c in cards:
    embedded[c["u"]] = dict(p=c["p"], d=c["d"], a=c["a"], z=c["z"])
    labels[c["u"]] = c.get("n") or f"{c.get('f')}–{c.get('to')}"
ltd_index = []
for r in ltd:
    k = "route/" + r[6]
    embedded[k] = dict(p=r[2], d=hm(r[3]), a=r[4], z=r[5])
    labels[k] = f"{r[0]}–{r[1]}"
    ltd_index.append(k)

if INIT:
    data = {"asof": None, "items": embedded}
    with open(OUT, "w", encoding="utf-8") as f:
        f.write('{"asof":null,"items":{\n' + ",\n".join(
            f'{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
            for k, v in embedded.items()) + "\n}}\n")
    print(f"已用 html 內嵌資料建立 {OUT}（{len(embedded)} 筆）")
    sys.exit(0)

# 以現有 prices.json 為基準（沒有就用 html 內嵌值）
current = dict(embedded)
if os.path.exists(OUT):
    try:
        current.update(json.load(open(OUT, encoding="utf-8")).get("items", {}))
    except Exception as e:
        print(f"::warning::讀取 prices.json 失敗，改用 html 內嵌值：{e}")


# ---------- 抓取 ----------
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
        if code == "200" and len(body) > 20000 and not final.rstrip("/").endswith("/japan-rail"):
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


today = datetime.date.today()
slice_no = today.toordinal() % SLICES
paths = [k for k in embedded if k not in set(ltd_index)]
paths += [k for i, k in enumerate(ltd_index) if i % SLICES == slice_no]
print(f"共 {len(paths)} 個頁面要檢查" + (f"（長尾路線今天更新第 {slice_no + 1}/{SLICES} 份）" if SLICES > 1 else ""), flush=True)


def run(path):
    h = fetch(path)
    return path, (parse(h) if h else None)


results = []
with ThreadPoolExecutor(WORKERS) as ex:
    for n, r in enumerate(ex.map(run, paths), 1):
        results.append(r)
        if n % 100 == 0:
            print(f"  已完成 {n}/{len(paths)}", flush=True)

failed = [p for p, r in results if r is None or r["p"] is None]
if len(failed) / len(paths) > MAX_FAIL_RATE:
    print(f"::error::{len(failed)}/{len(paths)} 個頁面抓取失敗，超過門檻，這次不更新")
    sys.exit(1)

changes, skipped = [], []
items = dict(current)
for path, r in results:
    if r is None or r["p"] is None:
        continue
    old = current[path]
    if not old["p"] or abs(r["p"] - old["p"]) / old["p"] > MAX_JUMP:
        skipped.append(f"{labels[path]}: {old['p']} → {r['p']}（差異過大，略過）")
        continue
    new = dict(p=r["p"], d=hm4(r["d"]) if r["d"] else old["d"],
               a=r["a"] or old["a"], z=r["z"] or old["z"])
    if new != old:
        items[path] = new
        if new["p"] != old["p"]:
            changes.append(f"{labels[path]}: NT${old['p']} → NT${new['p']}")

# ---------- 寫檔 ----------
text = '{"asof":"%s","items":{\n' % today.isoformat() + ",\n".join(
    f'{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
    for k, v in items.items()) + "\n}}\n"
if not DRY:
    open(OUT, "w", encoding="utf-8").write(text)

lines = ["## 價格更新報告",
         f"- 檢查頁面：{len(paths)}，抓取失敗：{len(failed)}",
         f"- 價格有變動：{len(changes)} 筆；{'（--dry-run，未寫檔）' if DRY else '已更新 prices.json（資料日期 ' + today.isoformat() + '）'}"]
if changes:
    lines += ["", "### 價格變動（前 60 筆）"] + [f"- {x}" for x in changes[:60]]
    if len(changes) > 60:
        lines.append(f"- …另外還有 {len(changes) - 60} 筆")
if skipped:
    lines += ["", "### 差異過大，已略過（請人工確認）"] + [f"- {x}" for x in skipped]
if failed:
    lines += ["", "### 抓取失敗，沿用舊資料"] + [f"- {p}" for p in failed[:40]]
print("\n".join(lines))
