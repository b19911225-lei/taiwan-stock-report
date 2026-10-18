# Routine 每日流程

> **目前停用（2026-10-18）**：每日報告改成只用 GitHub Actions 的免費額度執行
> （`.github/workflows/stock_report.yml`，一天排 5 次，第一個跑到的發報告），不扣訂閱用量也不扣雲端點數。
> Claude 排程裡的「台股選股日報」已停用，想恢復時在排程列表打開即可。下面保留原本的設計供參考。
>
> 實測的計費方式：Routine 執行扣**訂閱用量**（計費類型 `five_hour`）；手動建立的雲端 session，
> 包括之後被排程叫醒的每一輪，都扣**雲端點數**（`ccr_promotional`）。用工具建立的 Routine 沒有掛 repo，
> 拿不到 GitHub 工具，模式 B 無法觸發 Actions，要在 Routine 設定裡選這個 repo 才行。
>
> 目前的「準時觸發」：雲端 session「台股選股日報 - 每日觸發（點數）」在交易日 17:52 被排程叫醒，
> 用 GitHub 工具觸發 `stock_report.yml`，每次約 0.1 美元點數；排到 2026-11-04，當天 18:30 自動停用。
> 不要在排程頁面按 ▷ 手動執行：手動執行會另開一個沒掛 repo 的新 session，扣訂閱用量又觸發不了（見 PITFALLS）。
> 舊的「台股選股日報 - 每日觸發」（觸發不存在的 `daily_report.yml`、指令裡有權杖明文）已在 2026-10-18 刪除。

每個交易日 17:53（台北時間），Claude Code 的 Routine 會開一個新的雲端 session。它有兩種跑法：

```
模式 B（預設，不用設定任何金鑰）
  Routine 17:53 ──觸發──▶ GitHub Actions「台股選股日報」
                              check-date → collect → Claude API 寫 AI 文字 → finalize
                              （用 repo 的 Secrets；送 Telegram、存回 claude/report-history）

模式 A（雲端環境設好環境變數和網路白名單時）
  Routine 17:53：check-date → collect → Routine 的 Claude 自己寫 AI 文字 → validate-ai → finalize

兩種都一樣：GitHub Actions 21:07 再檢查一次，已經發過就跳過，沒發成或不完整才補發
```

- 模式 B 的好處：金鑰都在 GitHub Secrets，雲端環境不用設任何東西；資料由 GitHub 的主機抓，
  已實測連得到 FinMind、證交所、鉅亨網。AI 文字用 Claude API，實測每天約 0.02 美元。
- 模式 A 的好處：AI 文字由 Routine 自己寫，扣訂閱用量，不用 API 費用。需要在雲端環境設定金鑰和網路白名單（見最下面）。

---

## 給 Routine session 的步驟

> 你是排程執行的 session，沒有人在電腦前。照步驟做，遇到錯誤就回報，不要自己改程式。

### 0. 決定模式

檢查環境變數 `FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 是否**三個都有設定**（只檢查有沒有，**不要印出值**）：

```bash
for v in FINMIND_TOKEN TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do [ -n "${!v}" ] && echo "$v=已設定" || echo "$v=未設定"; done
```

- 三個都有 → 模式 A（第 A1 步開始）。
- 有任何一個沒設定 → 模式 B（第 B1 步）。

### 模式 B

**B1.** 用 GitHub 工具觸發 workflow（不需要 clone repo）：

- 工具：`mcp__github__actions_run_trigger`
- 參數：`method: run_workflow`、`owner: b19911225-lei`、`repo: taiwan-stock-report`、`workflow_id: stock_report.yml`、`ref: main`
- 如果工具說沒有權限或找不到 repo，先用 `add_repo` 加入 `b19911225-lei/taiwan-stock-report`（access: push）再試一次。

**B2.** 用 `mcp__github__actions_list`（`method: list_workflow_runs`、`resource_id: stock_report.yml`、`perPage: 1`）確認最新一筆 run 是剛剛觸發的（`event: workflow_dispatch`，狀態 queued 或 in_progress）。

**B3.** 回報觸發成功與 run 的網址後就結束，**不用等它跑完**。報告由 GitHub Actions 送出；失敗時 Actions 會自己發 Telegram 告警，21:07 的排程也會再試一次。

### 模式 A

**A1. 準備**

```bash
cd taiwan-stock-report 2>/dev/null || git clone https://github.com/b19911225-lei/taiwan-stock-report && cd taiwan-stock-report
git fetch origin main && git checkout --detach origin/main
PY=/usr/bin/python3.13
$PY -c "import pandas, numpy, requests" || $PY -m pip install --user -r requirements.txt
```

一律用 `/usr/bin/python3.13`。雲端主機的 `python3` 可能指到沒有裝套件的 3.11。

**A2. 今天要不要發**

```bash
$PY run_daily.py check-date --wait-minutes 8
```

Bash 的 timeout 設 600000。看結束代碼：

| 代碼 | 意思 | 接下來 |
|---|---|---|
| 0 | 交易日，資料已更新 | 繼續 A3 |
| 10 | 非交易日（週末不發；平日沒有資料會自動發一行休市通知） | 結束 |
| 11 | 今天已經發過 | 結束 |
| 12 | 還不到 17:00，資料可能還沒更新（不判定休市） | 結束 |
| 2 | 錯誤 | 改走模式 B（B1），並在回報裡寫出錯誤訊息 |

**A3. 抓資料、算數字**

```bash
$PY run_daily.py collect
```

timeout 設 600000。產生 `history/<日期>/picks.json`（所有數字的來源）和 `history/<日期>/ai_input.json`。非 0 就改走模式 B。

**A4. 寫 AI 文字**

讀 `docs/AI_WRITING_RULES.md` 和 `history/<日期>/ai_input.json`，用 Write 工具寫 `history/<日期>/ai_output.json`。

- 只能用 `ai_input.json` 裡的資料，**不要上網查**，也不要改 `picks.json`。
- `candidates` 是空的就寫 `"stocks": []`，`highlights` 可以只寫大盤和主題的重點。

**A5. 檢查 AI 文字**

```bash
$PY run_daily.py validate-ai
```

出現 `AI_INVALID` 就照訊息修改 `ai_output.json` 再檢查，最多 3 輪。還是不合格也繼續：finalize 會自動拿掉不合格的段落。

**A6. 核對、發送、存檔**

```bash
$PY run_daily.py finalize
```

timeout 設 600000。

| 代碼 | 意思 |
|---|---|
| 0 | `SENT` 成功；`SENT_INCOMPLETE` 是資料不完整也先發了，21:07 會補發完整版 |
| 3 | Telegram 發送失敗 → 改走模式 B |
| 4 | 已發送，但紀錄沒有存回 GitHub（21:07 可能會重發） |
| 11 | 已經發過 |

這一步會自動把 `history/` 推到 `claude/report-history` 分支。

### 最後回報

用幾行寫：用了哪個模式、日期、結果（模式 B 寫 run 網址；模式 A 寫觀察清單幾檔、AI 檢查結果、報告「資料檢查」區塊的內容、歷史分支有沒有推送成功）。

### 規則

- 不要修改程式碼、不要開 PR、不要推送到 `main` 或其他分支。唯一會推送的是 `run_daily.py` 自動推的 `claude/report-history`。
- 不要印出、寫入或回報任何 token。
- 程式出錯不要自己修，把錯誤訊息和結束代碼回報就好，下次在開發 session 修。

---

## 給設定的人

### 模式 B（預設）

不用設定。GitHub repo 的 Secrets 已經有 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`FINMIND_TOKEN`、`ANTHROPIC_API_KEY`。
AI 文字的模型可以用 `CLAUDE_MODEL` 覆寫（預設 `claude-sonnet-5-5`）。

### 想改用模式 A 的話

設定完要新開的 session 才會生效（文章坑 2）。

1. **網路白名單**：Claude 的雲端環境設定（session 標題列的環境選單 → Edit → Network access）改成 Limited（舊版是 Custom），Allowed domains 加入：

   | 網域 | 用途 |
   |---|---|
   | `api.finmindtrade.com` | 股價、法人、加權指數、官方股票清單 |
   | `www.twse.com.tw` | 當日成交量排行 |
   | `openapi.twse.com.tw` | 核對收盤價 |
   | `api.cnyes.com` | 鉅亨網台股新聞 |
   | `api.telegram.org` | 推送報告 |

   「Allow package managers」保持勾選。台灣的網站可能會擋國外的主機（文章坑 1）；被擋時報告會寫在「資料檢查」，Routine 也會改走模式 B。

2. **環境變數**：同一個設定頁的 Environment variables 加入 `FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。
   這些值「任何使用這個環境的人都看得到」；Telegram token 在網址裡，沒辦法用 Network secrets 保護（文章坑 4），建議用專用、可以隨時撤銷的 bot。

### 資料源有問題時

GitHub Actions 的「資料源檢查」會對 FinMind、證交所、鉅亨網、Telegram 各抓一次真實資料，確認格式和程式的假設一致，
再用最近一個交易日跑一次整個流程（不發送）。手動執行時也會測 Claude API 撰寫。改到 `data_fetcher.py` 時會自動跑。

### 手動測試

- 在 Claude 的排程列表裡手動執行一次 Routine，點進那次執行看 session 最後的回報（綠燈只代表主機正常結束，不代表報告發成功，文章坑 10）。
- 在那次執行裡輸入 `/usage` 可以看這次花了多少（文章坑 8）。
- 只想看報告長怎樣、不發送：`python3.13 run_daily.py all --dry-run --no-git`。
