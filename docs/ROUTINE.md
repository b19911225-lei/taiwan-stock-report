# Routine 每日流程

每個交易日 17:53（台北時間），Claude Code 的 Routine 會開一個新的雲端 session，照這份文件跑完整個流程。
GitHub Actions 的「台股選股日報（備援）」在 21:07 再檢查一次：Routine 已經發過就跳過，沒發成才補發。

```
Routine（17:53，扣訂閱用量）
  check-date → collect → Claude 寫 ai_output.json → validate-ai → finalize
                 │                                                   │
          程式抓資料、算好所有數字                        核對、送 Telegram、存回 claude/report-history
GitHub Actions 備援（21:07）
  已發過 → 結束；沒發 → 同一套流程，AI 文字改用 Claude API
```

---

## 給 Routine session 的步驟

> 你是排程執行的 session，沒有人在電腦前。照步驟做，遇到錯誤就回報，不要自己改程式。

### 0. 準備

```bash
cd taiwan-stock-report 2>/dev/null || git clone https://github.com/b19911225-lei/taiwan-stock-report && cd taiwan-stock-report
git fetch origin main && git checkout --detach origin/main
PY=/usr/bin/python3.13
$PY -c "import pandas, numpy, requests" || $PY -m pip install --user -r requirements.txt
```

- 一律用 `/usr/bin/python3.13`。雲端主機的 `python3` 可能指到沒有裝套件的 3.11。
- 確認環境變數 `FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 有設定（只檢查有沒有，**不要印出值**）。

### 1. 今天要不要發

```bash
$PY run_daily.py check-date --wait-minutes 8
```

Bash 的 timeout 設 600000。看結束代碼：

| 代碼 | 意思 | 接下來 |
|---|---|---|
| 0 | 交易日，資料已更新 | 繼續第 2 步 |
| 10 | 非交易日（週末不發；平日沒有資料會自動發一行休市通知） | 結束 |
| 11 | 今天已經發過 | 結束 |
| 2 | 錯誤（通常是網路白名單沒開，連不到 FinMind） | 回報錯誤訊息，結束 |

### 2. 抓資料、算數字

```bash
$PY run_daily.py collect
```

timeout 一樣設 600000。產生 `history/<日期>/picks.json`（所有數字的來源）和 `history/<日期>/ai_input.json`。非 0 就回報並結束。

### 3. 寫 AI 文字

讀 `docs/AI_WRITING_RULES.md` 和 `history/<日期>/ai_input.json`，用 Write 工具寫 `history/<日期>/ai_output.json`。

- 只能用 `ai_input.json` 裡的資料，**不要上網查**，也不要改 `picks.json`。
- `candidates` 是空的就寫 `"stocks": []`，`highlights` 可以只寫大盤和主題的重點。

### 4. 檢查 AI 文字

```bash
$PY run_daily.py validate-ai
```

出現 `AI_INVALID` 就照訊息修改 `ai_output.json` 再檢查，最多 3 輪。還是不合格也繼續下一步：finalize 會自動拿掉不合格的段落，不會送出。

### 5. 核對、發送、存檔

```bash
$PY run_daily.py finalize
```

timeout 設 600000。`SENT`（代碼 0）就是成功；代碼 3 是 Telegram 發送失敗，回報錯誤。
這一步會自動把 `history/` 推到 `claude/report-history` 分支。

### 6. 回報

最後一則訊息用幾行寫：日期、觀察清單幾檔、AI 檢查結果（幾處不合格）、報告「資料檢查」區塊的內容、歷史分支有沒有推送成功。

### 規則

- 不要修改程式碼、不要開 PR、不要推送到 `main` 或其他分支。唯一會推送的是 `run_daily.py` 自動推的 `claude/report-history`。
- 不要印出、寫入或回報任何 token。
- 程式出錯不要自己修，把錯誤訊息和結束代碼回報就好，下次在開發 session 修。

---

## 給設定的人：第一次設定

Routine 用的是這個 repo 所在的雲端環境（目前是「Default」）。第一次要設定兩件事，設定完要新開的 session 才會生效（文章坑 2）。

### 1. 網路白名單

雲端環境預設的 Trusted 網路連不到 FinMind 和證交所。到 Claude 的雲端環境設定（session 標題列的環境選單 → Edit → Network access），改成 Custom（或 Limited），在 Allowed domains 加入：

| 網域 | 用途 |
|---|---|
| `api.finmindtrade.com` | 股價、法人、加權指數、官方股票清單 |
| `www.twse.com.tw` | 當日成交量排行 |
| `openapi.twse.com.tw` | 核對收盤價 |
| `api.cnyes.com` | 鉅亨網台股新聞 |
| `api.telegram.org` | 推送報告 |

「Allow package managers」保持勾選（需要時才能 `pip install`）。

> 台灣的網站可能會擋國外的主機（文章坑 1）。程式會偵測擋頁，被擋的資料會在報告「資料檢查」區塊寫出來，不會自己補。

### 2. 環境變數

在同一個環境設定的 Environment variables 加入：

```
FINMIND_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

- Routine 模式**不需要** `ANTHROPIC_API_KEY`：AI 文字由 Routine 的 Claude 自己寫，扣的是訂閱用量。
- 環境變數「任何使用這個環境的人都看得到」。Telegram token 放在網址裡，沒辦法用 Network secrets 保護（文章坑 4），建議用一個專用、可以隨時撤銷的 bot。
- `FINMIND_TOKEN` 是放在 header 的，也可以改用環境設定裡的 Network secrets（Bearer）給 `api.finmindtrade.com`，就不用設這個環境變數。

### 3. GitHub Actions 備援

repo 的 Secrets 已經有 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`FINMIND_TOKEN`、`ANTHROPIC_API_KEY`，不用改。備援跑的時候才會呼叫 Claude API（模型可用 `CLAUDE_MODEL` 環境變數覆寫，預設 `claude-sonnet-5-5`）。

### 4. 手動測試

- 在 Claude 的排程列表裡手動執行一次 Routine，點進那次執行看 session 最後的回報（綠燈只代表主機正常結束，不代表報告發成功，文章坑 10）。
- 在那次執行裡輸入 `/usage` 可以看這次花了多少（文章坑 8）。
- 只想看報告長怎樣、不發送：`python3.13 run_daily.py all --dry-run --no-git`。
