"""
嵯峨野觀光小火車 搶票輔助腳本

功能：
  python bot.py login   開啟瀏覽器讓你「自己」登入訂票網站（只需做一次，登入狀態存在 browser-profile/）
  python bot.py check   查詢目前符合條件的座位（不開瀏覽器、不訂票）
  python bot.py run     等到開賣時間 → 自動找班次 → 挑座位 → 開到付款頁，停下來讓你自己確認付款

安全設計：
  - 腳本不儲存、不輸入任何密碼或信用卡資料。登入和付款都由你本人在瀏覽器裡操作。
  - 查詢頻率預設 1 秒 1 次、最多 15 分鐘，避免對官方伺服器造成負擔。
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
API = "https://common-api.sagano.linktivity.io"
BOOKING_SITE = "https://ars-saganokanko.triplabo.jp"
PRODUCT_ID = "51"
JST = timezone(timedelta(hours=9))

STATIONS = {  # 官方 API 的 station_id
    "saga": "1",
    "arashiyama": "2",
    "hozukyo": "3",
    "kameoka": "4",
}

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://file.sagano.linktivity.io",
    "Referer": "https://file.sagano.linktivity.io/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}


# ---------------------------------------------------------------- config
def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["from_id"] = STATIONS[cfg["from_station"]]
    cfg["to_id"] = STATIONS[cfg["to_station"]]
    p = cfg["passengers"]
    cfg["total"] = int(p.get("adult", 0)) + int(p.get("child", 0))
    if not 1 <= cfg["total"] <= 8:
        sys.exit("每筆訂單人數需為 1~8 人")
    groups = cfg["seat_rule"].get("seat_groups")
    if groups and sum(len(g) for g in groups) != cfg["total"]:
        sys.exit(f"seat_groups 的座位總數 ({sum(len(g) for g in groups)}) 要等於人數 ({cfg['total']})")
    return cfg


def direction(cfg) -> str:
    return "down" if int(cfg["from_id"]) < int(cfg["to_id"]) else "up"


def sale_open_time(target: date) -> datetime:
    """開賣時間：搭乘日前一個月同一天 0:00 (日本時間)。"""
    y, m = (target.year, target.month - 1) if target.month > 1 else (target.year - 1, 12)
    d = target.day
    while True:  # 例：3/31 → 2/28
        try:
            return datetime(y, m, d, tzinfo=JST)
        except ValueError:
            d -= 1


# ---------------------------------------------------------------- API
session = requests.Session()
session.headers.update(HEADERS)


def search_services(cfg) -> tuple[list[dict], str]:
    r = session.post(
        f"{API}/v1/search-inventory",
        json={
            "date": cfg["date"],
            "from_station_id": cfg["from_id"],
            "to_station_id": cfg["to_id"],
            "product_id": PRODUCT_ID,
            "total": cfg["total"],
        },
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    return j.get("services") or [], j.get("hint_type", "")


def get_inventory(cfg, service_id: str) -> dict:
    r = session.get(
        f"{API}/v1/inventories/{cfg['date']}/services/{service_id}",
        params={"product_id": PRODUCT_ID, "base_booking_id": ""},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- seats
@dataclass
class Seat:
    car: int  # 幾號車（physical）
    logical_car_id: str
    row: int
    letter: str
    type_id: str

    @property
    def label(self) -> str:
        return f"{self.car}號車 {self.row:02d}{self.letter}"

    @property
    def url_token(self) -> str:
        return f"{self.logical_car_id}-{self.row:02d}-{self.letter}-{self.type_id}"


def candidate_seats(cfg, inv: dict) -> list[Seat]:
    """可用車廂裡的空位。用 seat_groups 時回傳全部空位（單雙排、座位字母由 pick_seat_groups 處理）。"""
    rule = cfg["seat_rule"]
    use_groups = bool(rule.get("seat_groups"))
    out = []
    for car in inv.get("car_inventories", []):
        car_no = int(car["physical_car_id"])
        if car_no not in rule["cars"]:
            continue
        for a in car.get("arrangements", []):
            if a.get("arrangement_state") != "ARRANGEABLE" or a.get("reservation_state") != "VACANT":
                continue
            if not str(a.get("seat_group_id", "")).isdigit():  # 例如輪椅席 "wheelchair"
                continue
            row = int(a["seat_group_id"])
            if not use_groups and rule.get("even_rows_only") and row % 2:
                continue
            if not use_groups and a["seat_id"] not in rule["seat_letters"]:
                continue
            out.append(Seat(car_no, car["logical_car_id"], row, a["seat_id"], a["arrangement_type_id"]))
    return out


def pick_seat_groups(cfg, seats: list[Seat]) -> list[Seat] | None:
    """依 seat_groups 挑座位：每組要在「同一排」湊齊指定座位，例如 [["A","C","D"], ["A","D"]]。
    各組分在不同排。優先順序：
      1. 雙數排（even_rows_only=true 時只用雙數排；allow_odd_rows_fallback=true 時湊不齊才用奇數排）
      2. 同一節車廂（prefer_same_car）
      3. 排數越大越好（prefer_high_rows）
      4. car_priority 排越前面的車廂越好，例如 [4, 3, 2, 1]
      5. 排與排越近越好
    都湊不齊、且 allow_any_seats_fallback=true 時，改挑任意空位（pick_any_seats）。"""
    rule = cfg["seat_rule"]
    groups = rule["seat_groups"]
    car_rank = car_ranks(rule)
    high_rows = rule.get("prefer_high_rows", False)
    allow_odd = not rule.get("even_rows_only") or rule.get("allow_odd_rows_fallback")
    rows: dict[tuple[int, int], dict[str, Seat]] = {}
    for s in seats:
        if s.row % 2 and not allow_odd:
            continue
        rows.setdefault((s.car, s.row), {})[s.letter] = s

    # 每一組可以放在哪些排
    fits = [[k for k, v in rows.items() if all(l in v for l in g)] for g in groups]

    best = None

    def search(i, used, picked):
        nonlocal best
        if i == len(groups):
            cars = {k[0] for k in picked}
            nums = [k[1] for k in picked]
            score = (sum(n % 2 for n in nums),
                     len(cars) if rule.get("prefer_same_car", True) else 0,
                     sorted(-n for n in nums) if high_rows else [],
                     sorted(car_rank.get(c, 99) for c in cars),
                     max(nums) - min(nums), min(picked))
            if best is None or score < best[0]:
                best = (score, list(picked))
            return
        for k in fits[i]:
            if k not in used:
                search(i + 1, used | {k}, picked + [k])

    search(0, frozenset(), [])
    if best:
        return [rows[k][l] for k, g in zip(best[1], groups) for l in g]
    if rule.get("allow_any_seats_fallback"):
        print("  湊不齊指定的座位組合，改挑任意空位")
        return pick_any_seats(cfg, seats)
    return None


def car_ranks(rule) -> dict[int, int]:
    return {c: i for i, c in enumerate(rule.get("car_priority") or sorted(rule["cars"]))}


def pick_any_seats(cfg, seats: list[Seat]) -> list[Seat] | None:
    """任意空位：優先同一節車廂（依 car_priority），排數越大越好。"""
    n = cfg["total"]
    if len(seats) < n:
        return None
    rank = car_ranks(cfg["seat_rule"])
    key = lambda s: (-s.row, s.letter)
    by_car: dict[int, list[Seat]] = {}
    for s in seats:
        by_car.setdefault(s.car, []).append(s)
    for car in sorted(by_car, key=lambda c: rank.get(c, 99)):
        if len(by_car[car]) >= n:
            return sorted(by_car[car], key=key)[:n]
    return sorted(seats, key=lambda s: (-s.row, rank.get(s.car, 99), s.letter))[:n]


def pick_seats(cfg, seats: list[Seat]) -> list[Seat] | None:
    """挑 N 個座位：優先同一節車廂、排數越集中越好。"""
    if cfg["seat_rule"].get("seat_groups"):
        return pick_seat_groups(cfg, seats)
    n = cfg["total"]
    if len(seats) < n:
        return None

    def best_window(pool: list[Seat]):
        pool = sorted(pool, key=lambda s: (s.row, s.letter))
        best = None
        for i in range(len(pool) - n + 1):
            win = pool[i : i + n]
            span = win[-1].row - win[0].row
            if best is None or span < best[0]:
                best = (span, win)
        return best

    if cfg["seat_rule"].get("prefer_same_car", True):
        by_car: dict[int, list[Seat]] = {}
        for s in seats:
            by_car.setdefault(s.car, []).append(s)
        options = [best_window(v) for v in by_car.values() if len(v) >= n]
        options = [o for o in options if o]
        if options:
            return min(options, key=lambda o: o[0])[1]
    # 同車不夠 → 跨車廂，依車廂、排數排序取前 N 個最集中的
    seats = sorted(seats, key=lambda s: (s.car, s.row, s.letter))
    return seats[:n]


def booking_url(cfg, service_id: str, chosen: list[Seat]) -> str:
    seats = ",".join(s.url_token for s in chosen)
    return (
        f"{BOOKING_SITE}/booking/pay?fromStationId={cfg['from_id']}&toStationId={cfg['to_id']}"
        f"&serviceId={service_id}&seats={quote(seats, safe=',-')}"
    )


def manual_seat_url(cfg) -> str:
    """備援：官方選位頁（自己手動點座位）。"""
    return (
        f"https://file.sagano.linktivity.io/seat/{PRODUCT_ID}/{direction(cfg)}?lang=ja"
        f"&date={cfg['date']}&unitsCount={cfg['total']}"
        f"&backUrl={quote(BOOKING_SITE + '/activity/ja/LINKTIVITY-YRBTL', safe='')}"
        f"&redirectUrl={quote(BOOKING_SITE + '/booking/pay', safe='')}&currentStep=station"
    )


ACTIVITY_URL = f"{BOOKING_SITE}/activity/ja/LINKTIVITY-YRBTL"


def prepare_booking_session(page, cfg) -> bool:
    """在商品頁自動選「日期 → 方向 → 人數 → 予約手続きへ」。
    付款頁需要這一步存在 sessionStorage 的訂票資料（同一分頁才有效），沒有的話會被導回首頁。"""
    target = date.fromisoformat(cfg["date"])
    page.goto(ACTIVITY_URL, wait_until="domcontentloaded")  # 不等大圖片載完
    # 視窗寬時日曆會並排顯示兩個月，所以找出標題是目標月份的那一個
    month_label = f"{target.month}月 {target.year}"
    page.locator("[class*=DateTable_dateTableCurrent]").first.wait_for(timeout=15000)
    month_table = page.locator("[class*=DateTable_dateTable_]").filter(
        has=page.locator("[class*=DateTable_dateTableCurrent]", has_text=month_label))
    for _ in range(3):
        if month_table.count():
            break
        page.locator("[class*=DateTable_dateTableNext]:visible").last.click()
        page.wait_for_timeout(300)
    else:
        print("  商品頁找不到月份", month_label)
        return False

    day_btn = month_table.first.locator("[class*=DateTable_dateTableDayButton]").filter(
        has_text=re.compile(rf"^\s*{target.day}\s*$"))
    if not day_btn.count() or day_btn.first.is_disabled():
        print(f"  商品頁的 {target.month}/{target.day} 還不能選（可能尚未開賣）")
        return False
    day_btn.first.click()

    plan_text = "亀岡駅　→" if direction(cfg) == "up" else "嵯峨駅／嵐山駅　→"
    page.locator("[class*=PlanPicker_planPicker]").filter(has_text=plan_text).first.click(timeout=10000)

    rows = page.locator("[class*=SelectUnit_amount_]")
    rows.first.wait_for(timeout=10000)
    for idx, key in enumerate(("adult", "child")):
        plus = rows.nth(idx).locator("[class*=InputNumber_button]").nth(1)
        for _ in range(int(cfg["passengers"].get(key, 0))):
            plus.click()

    page.get_by_role("button", name="予約手続きへ").click()
    page.wait_for_url("**file.sagano.linktivity.io/**", timeout=15000, wait_until="commit")
    return True


STATION_NAMES = {  # 官方選位頁上的站名
    "saga": "トロッコ嵯峨",
    "arashiyama": "トロッコ嵐山",
    "hozukyo": "トロッコ保津峡",
    "kameoka": "トロッコ亀岡",
}


def select_on_seat_page(page, cfg, chosen: list[Seat]) -> bool:
    """在官方選位頁照真人流程操作：選車站 → 選班次 → 點座位 → 確認 → 次へ。
    最後的「次へ」會讓網站自己呼叫確認座位 API，成功後跳到付款頁。"""
    page.set_default_timeout(15000)
    combo = page.locator("[role=combobox][tabindex]")
    combo.first.wait_for()
    for i, key in enumerate(("from_station", "to_station")):
        combo.nth(i).click()
        page.locator("[role=option] button", has_text=STATION_NAMES[cfg[key]]).click()

    time_re = re.compile(rf"^\s*{re.escape(cfg['departure'])}\s*$")
    page.locator("button[class*=_train_]").filter(has=page.locator("p", has_text=time_re)).first.click()
    page.get_by_role("button", name="次へ").click()

    current_car = None
    for st in sorted(chosen, key=lambda s: s.car):
        if st.car != current_car:  # 展開該節車廂
            page.locator("button[class*=_carriageContainer_]", has_text=f"{st.car}号車").click()
            current_car = st.car
        row = page.locator("[class*=_group_]").filter(
            has=page.locator("[class*=_groupId_]", has_text=re.compile(rf"^0?{st.row}$")))
        row.locator("button").filter(
            has=page.locator("span", has_text=re.compile(rf"^{st.letter}$"))).first.click()

    page.get_by_role("button", name="次へ").click()  # → 座席の確認
    page.wait_for_url("**currentStep=confirm**", wait_until="commit")
    page.get_by_role("button", name="次へ").click()  # → 網站確認座位後跳付款頁
    try:
        page.wait_for_url("**/booking/pay**", timeout=20000, wait_until="commit")
    except Exception:
        print("  確認座位沒有通過（可能剛被別人訂走）")
        return False
    return True


def load_profile() -> dict:
    """profile.json（不上傳 GitHub）：{"last_name": "HSU", "first_name": "..."}"""
    p = ROOT / "profile.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fill_participant(page, profile: dict) -> bool:
    """在付款頁「参加者情報」填姓、名（只填欄位，不按確定）。"""
    if not (profile.get("last_name") and profile.get("first_name")):
        return False
    inputs = page.locator("input[type=text]:visible, input:not([type]):visible")
    inputs.first.wait_for(timeout=15000)
    for label, value in (("姓", profile["last_name"]), ("名", profile["first_name"])):
        lab = page.get_by_text(re.compile(rf"^\W*{label}\s*[(（]半角英字")).first
        box = lab.locator("xpath=following::input[1]")
        if not box.count():
            box = inputs.nth(0 if label == "姓" else 1)
        box.fill(value.upper())
        box.blur()
    return True


def confirm_booking(page) -> bool:
    """按「予約を確定する」（建立訂單、保留座位，還不會扣款），等到「お支払い」步驟。
    同意條款的勾選和付款都留給本人。"""
    page.get_by_role("button", name="予約を確定する").click()
    try:
        page.get_by_role("button", name="支払いへ").wait_for(timeout=20000)
        return True
    except Exception:
        return False


def agree_and_go_to_payment(page) -> bool:
    """勾選「上記を読んだ上で同意しました」→ 按「支払いへ」，停在信用卡輸入頁（不填卡號、不付款）。"""
    page.get_by_text("上記を読んだ上で同意しました").click()
    btn = page.get_by_role("button", name="支払いへ")
    btn.wait_for()
    if btn.is_disabled():
        return False
    btn.click()
    try:
        page.wait_for_url("**payment.linktivity.io/**", timeout=20000, wait_until="commit")
        return True
    except Exception:
        return False


def open_manual_seat_page(page, cfg):
    """手動備援：一樣先走商品頁（付款頁才有訂票資料），再停在選位頁讓你自己點。"""
    try:
        if prepare_booking_session(page, cfg):
            return
    except Exception as e:
        print("  商品頁自動操作失敗：", str(e).splitlines()[0])
    print("  請在商品頁自己選日期、人數後按「予約手続きへ」。")
    page.goto(ACTIVITY_URL)


# ---------------------------------------------------------------- flow
def find_service(cfg, services: list[dict]) -> dict | None:
    for s in services:
        if s.get("departure_hhmm") == cfg["departure"]:
            return s
    return None


def try_once(cfg, verbose=True):
    services, hint = search_services(cfg)
    if not services:
        if verbose:
            print(f"  尚無班次（{hint or 'no services'}）")
        return None
    svc = find_service(cfg, services)
    if not svc:
        print("  找不到出發時間", cfg["departure"], "的班次。可選：",
              [s.get("departure_hhmm") for s in services])
        return None
    if not svc.get("available", True):
        print("  該班次已無足夠座位")
        return None
    inv = get_inventory(cfg, svc["id"])
    cands = candidate_seats(cfg, inv)
    chosen = pick_seats(cfg, cands)
    name = svc.get("name", {}).get("short_name", "")
    print(f"  班次 {name} {svc['departure_hhmm']}→{svc.get('arrival_hhmm')} (serviceId={svc['id']})，"
          f"符合條件空位 {len(cands)} 個")
    if not chosen:
        print(f"  符合條件的座位不足 {cfg['total']} 個")
        return None
    return svc, chosen, cands


def cmd_check(cfg):
    print(f"查詢 {cfg['date']} {cfg['from_station']}→{cfg['to_station']} {cfg['departure']}，{cfg['total']} 人")
    res = try_once(cfg)
    if res:
        svc, chosen, cands = res
        print("  全部符合座位：", ", ".join(s.label for s in sorted(cands, key=lambda s: (s.car, s.row, s.letter))))
        print("  建議選擇：", ", ".join(s.label for s in chosen))
        print("  付款頁網址：", booking_url(cfg, svc["id"], chosen))


CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
]


def open_browser():
    """用「一般方式」啟動 Chrome，再讓 Playwright 連上去操作。
    不用 Playwright 自己啟動，是因為它會加 --no-sandbox 等自動化參數，
    導致信用卡 3D 驗證視窗載不出來（一片空白）。付款必須在同一個分頁完成，訂單才會成立。"""
    import socket
    import subprocess
    from playwright.sync_api import sync_playwright

    chrome = next((c for c in CHROME_PATHS if Path(c).exists()), None)
    if not chrome:
        sys.exit("找不到 Google Chrome，請先安裝。")
    with socket.socket() as sock:  # 找一個空的 port 給遠端控制用
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([
        chrome, f"--remote-debugging-port={port}", f"--user-data-dir={ROOT / 'browser-profile'}",
        "--no-first-run", "--no-default-browser-check", "--lang=ja", "--window-size=1100,950",
        "about:blank",
    ])

    pw = sync_playwright().start()
    browser = None
    for _ in range(30):  # 等 Chrome 開好
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    if not browser:
        pw.stop()
        sys.exit("無法連上 Chrome。常見原因：上一次開的程式瀏覽器視窗還沒關（browser-profile 被占用），"
                 "請先關掉再試一次。")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def wait_for_close(ctx):
    print("瀏覽器關閉後腳本結束（或按 Ctrl+C）。")
    try:
        while ctx.browser.is_connected() and ctx.pages:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def close_browser(pw, ctx):
    try:
        if ctx.browser.is_connected():
            ctx.browser.close()
    except Exception:
        pass
    pw.stop()


def cmd_login(cfg):
    pw, ctx, page = open_browser()
    page.goto(f"{BOOKING_SITE}/login")
    print("請在打開的瀏覽器裡「自己」登入（Google / Facebook / Email）。")
    print("登入完成後直接關閉瀏覽器，登入狀態會保存在 browser-profile/。")
    wait_for_close(ctx)
    close_browser(pw, ctx)


def cmd_run(cfg):
    target = date.fromisoformat(cfg["date"])
    open_at = sale_open_time(target)
    start_at = open_at - timedelta(seconds=cfg.get("start_before_open_sec", 5))
    now = datetime.now(JST)
    print(f"目標：{cfg['date']} {cfg['departure']} {cfg['from_station']}→{cfg['to_station']}，{cfg['total']} 人")
    print(f"開賣時間：{open_at:%Y-%m-%d %H:%M} 日本時間 "
          f"（台灣 {open_at.astimezone(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M}）")

    # 先開好瀏覽器、確認登入，開賣時省時間
    pw, ctx, page = open_browser()
    page.goto(f"{BOOKING_SITE}/home")
    print("已開啟瀏覽器。若右上角還沒登入，請現在先登入。")

    if now < start_at:
        print(f"等待中…將在 {start_at.astimezone(timezone(timedelta(hours=8))):%H:%M:%S}（台灣）開始查詢")
        while datetime.now(JST) < start_at:
            time.sleep(0.2)

    deadline = time.time() + cfg.get("poll_timeout_min", 15) * 60
    res = None
    while time.time() < deadline:
        try:
            print(datetime.now(JST).strftime("[%H:%M:%S JST] 查詢…"))
            res = try_once(cfg)
            if res:
                break
        except requests.RequestException as e:
            print("  連線錯誤：", e)
        time.sleep(cfg.get("poll_interval_sec", 1.0))

    if not res:
        print("時間內沒有搶到符合條件的座位，改開官方選位頁讓你手動選。")
        open_manual_seat_page(page, cfg)
    else:
        ok = False
        for attempt in range(1, 4):  # 座位被搶走時重新查詢再試，最多 3 次
            if attempt > 1:
                res = try_once(cfg)
                if not res:
                    break
            svc, chosen, _ = res
            print(f"選定座位（第 {attempt} 次）：", ", ".join(s.label for s in chosen))
            try:
                print("  商品頁：選日期、人數…")
                if not prepare_booking_session(page, cfg):
                    break
                if cfg.get("seat_mode", "click") == "direct":
                    # 商品頁花了幾秒，重查一次確保座位還是空的，再把座位直接帶進付款頁網址
                    res = try_once(cfg, verbose=False) or res
                    svc, chosen, _ = res
                    print("  直接帶座位到付款頁：", ", ".join(s.label for s in chosen))
                    page.goto(booking_url(cfg, svc["id"], chosen), wait_until="commit")
                    ok = True
                else:
                    print("  選位頁：選班次、點座位…")
                    ok = select_on_seat_page(page, cfg, chosen)
            except Exception as e:
                print("  自動操作失敗：", str(e).splitlines()[0])
            if ok:
                break
        if ok:
            filled = False
            try:
                filled = fill_participant(page, load_profile())
                if filled:
                    print("  已填入姓名（profile.json）")
            except Exception as e:
                print("  自動填姓名失敗，請自己填：", str(e).splitlines()[0])
            confirmed = False
            if filled and cfg.get("auto_confirm"):
                try:
                    confirmed = confirm_booking(page)
                except Exception as e:
                    print("  自動按「予約を確定する」失敗：", str(e).splitlines()[0])
                print("  已按「予約を確定する」，座位保留中" if confirmed
                      else "  沒有成功進到「お支払い」，請看瀏覽器畫面")
            to_payment = False
            if confirmed and cfg.get("auto_agree_and_proceed"):
                try:
                    to_payment = agree_and_go_to_payment(page)
                except Exception as e:
                    print("  自動勾選同意 / 按「支払いへ」失敗：", str(e).splitlines()[0])
            if to_payment:
                print("\n>>> 訂單已建立、座位保留中（還沒扣款），已開到信用卡付款頁。")
                print(">>> 請在「這個瀏覽器、這個分頁」填卡號、按「支払う」並完成銀行驗證，")
                print(">>> 最後出現綠色大勾勾才算預約完成。不要換到別的瀏覽器付款，訂單會無法成立。")
                print(">>> 付款期限內沒付，座位會被釋放。")
            elif confirmed:
                print("\n>>> 已建立訂單、保留座位（還沒扣款）。請在瀏覽器裡：")
                print(">>> 確認金額 → 勾選「上記を読んだ上で同意しました」→ 按「支払いへ」→ 自己付款。")
                print(">>> 付款期限內沒付款，座位會被釋放。")
            else:
                print("\n>>> 已開到付款頁。請在瀏覽器裡確認：日期、班次、座位、人數（大人/小孩），")
                print(">>> 然後由你自己勾選同意並完成付款。付款完成才算正式訂到座位。")
        else:
            print("\n自動選位沒有成功，改開官方選位頁，請手動選位。")
            open_manual_seat_page(page, cfg)
    wait_for_close(ctx)
    close_browser(pw, ctx)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    cfg = load_config()
    {"check": cmd_check, "login": cmd_login, "run": cmd_run}.get(cmd, lambda c: sys.exit(__doc__))(cfg)


if __name__ == "__main__":
    main()
