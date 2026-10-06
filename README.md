# sagano-ticket-bot

嵯峨野觀光小火車（嵐山小火車）搶票輔助腳本。自動在開賣時間查班次、挑座位、開到付款頁；**登入與付款由本人在瀏覽器完成**，腳本不碰密碼與信用卡。

## 安裝（Windows）

```powershell
cd sagano-ticket-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium   # 已裝 Google Chrome 可略過
```

## 設定 `config.json`

| 欄位 | 說明 |
|---|---|
| `date` | 搭乘日 `YYYY-MM-DD` |
| `from_station` / `to_station` | `saga` / `arashiyama` / `hozukyo` / `kameoka` |
| `departure` | 出發時間，例如 `09:30`（龜岡→嵐山 嵯峨野2號） |
| `passengers` | 大人 / 小孩人數（合計 1~8） |
| `seat_rule` | 只要雙數排、A/D 座位、可接受的車廂、是否優先同車廂 |

## 使用

```powershell
python bot.py login   # 第一次：自己登入，登入完關掉瀏覽器
python bot.py check   # 查目前符合條件的座位（不訂票）
python bot.py run     # 正式：等開賣 → 選位 → 開到付款頁 → 你自己付款
```

開賣時間：搭乘日前一個月同一天 **0:00 日本時間**（= 台灣前一天 23:00）。
例：11/30 → 10/29 23:00（台灣）開賣，建議 22:50 前執行 `python bot.py run`。

## 規則提醒
- 訂後不能改日期/班次/人數，只能取消重買；搭乘前一天取消免費，當天取消 100%。
- 座位可在前一天前改，最多 3 次。
- 付款完成才正式保留座位。
