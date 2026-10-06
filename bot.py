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
    rule = cfg["seat_rule"]
    out = []
    for car in inv.get("car_inventories", []):
        car_no = int(car["physical_car_id"])
        if car_no not in rule["cars"]:
            continue
        for a in car.get("arrangements", []):
            if a.get("arrangement_state") != "ARRANGEABLE" or a.get("reservation_state") != "VACANT":
                continue
            row = int(a["seat_group_id"])
            if rule.get("even_rows_only") and row % 2:
                continue
            if a["seat_id"] not in rule["seat_letters"]:
                continue
            out.append(Seat(car_no, car["logical_car_id"], row, a["seat_id"], a["arrangement_type_id"]))
    return out


def pick_seats(cfg, seats: list[Seat]) -> list[Seat] | None:
    """挑 N 個座位：優先同一節車廂、排數越集中越好。"""
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


def open_browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    kwargs = dict(user_data_dir=str(ROOT / "browser-profile"), headless=False,
                  viewport={"width": 1100, "height": 900}, locale="ja-JP")
    try:
        ctx = pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def wait_for_close(ctx):
    print("瀏覽器關閉後腳本結束（或按 Ctrl+C）。")
    try:
        while ctx.pages:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def cmd_login(cfg):
    pw, ctx, page = open_browser()
    page.goto(f"{BOOKING_SITE}/login")
    print("請在打開的瀏覽器裡「自己」登入（Google / Facebook / Email）。")
    print("登入完成後直接關閉瀏覽器，登入狀態會保存在 browser-profile/。")
    wait_for_close(ctx)
    ctx.close()
    pw.stop()


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
        page.goto(manual_seat_url(cfg))
    else:
        svc, chosen, _ = res
        print("選定座位：", ", ".join(s.label for s in chosen))
        url = booking_url(cfg, svc["id"], chosen)
        page.goto(url)
        print("\n>>> 已開到付款頁。請在瀏覽器裡確認：日期、班次、座位、人數（大人/小孩），")
        print(">>> 然後由你自己完成付款。付款完成才算正式訂到座位。")
        print(">>> 如果頁面顯示錯誤，請改用官方選位頁：", manual_seat_url(cfg))
    wait_for_close(ctx)
    try:
        ctx.close()
    finally:
        pw.stop()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    cfg = load_config()
    {"check": cmd_check, "login": cmd_login, "run": cmd_run}.get(cmd, lambda c: sys.exit(__doc__))(cfg)


if __name__ == "__main__":
    main()
