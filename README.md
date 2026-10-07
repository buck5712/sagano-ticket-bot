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
| `seat_mode` | `direct`：座位直接帶進付款頁網址（較快）；`click`：在官方選位頁一個個點座位 |
| `seat_rule.cars` | 可接受的車廂，例如 `[1,2,3,4]`（不要 5 號車） |
| `seat_rule.seat_groups` | 每組要在同一排湊齊，例如 `[["A","C","D"],["A","D"]]`；總數要等於人數。沒設定時改用 `seat_letters` |
| `seat_rule.even_rows_only` | 只要雙數排；搭配 `allow_odd_rows_fallback` 可在湊不齊時改用奇數排 |
| `seat_rule.allow_any_seats_fallback` | 都湊不齊時改挑任意空位 |
| `seat_rule.prefer_high_rows` / `car_priority` | 排數越大越好；車廂優先順序，例如 `[4,3,2,1]` |

挑位優先順序：雙數排 > 同一車廂 > 排數越大 > 車廂順序 > 奇數排 > 任意座位。

### 姓名 `profile.json`（不會上傳 GitHub）

```json
{ "last_name": "YOUR", "first_name": "NAME" }
```
有這個檔案時，`run` 開到付款頁會自動填好參加者姓名（只填欄位，不按確定）。

## 使用

```powershell
python bot.py login   # 第一次：自己登入，登入完關掉瀏覽器
python bot.py check   # 查目前符合條件的座位（不訂票）
python bot.py run     # 正式：等開賣 → 選位 → 商品頁自動選日期人數 → 開到付款頁並填好姓名 → 你自己付款
```

開賣時間：搭乘日前一個月同一天 **0:00 日本時間**（= 台灣前一天 23:00）。
例：11/30 → 10/29 23:00（台灣）開賣，建議 22:50 前執行 `python bot.py run`。

## 規則提醒
- 訂後不能改日期/班次/人數，只能取消重買；搭乘前一天取消免費，當天取消 100%。
- 座位可在前一天前改，最多 3 次。
- 付款完成才正式保留座位。
