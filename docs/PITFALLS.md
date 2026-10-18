# 踩坑紀錄

每遇到一件意外就記一列。新的寫在各段最上面。

## 資料與計算

| 日期 | 坑 | 怎麼發現 | 處理 |
|---|---|---|---|
| 2026-10-18 | **法人資料集名稱錯了**：`TaiwanStockInstitutionalInvestors` 回 HTTP 422（不存在），`_fetch` 失敗只回空表，所以從上線起一筆法人資料都沒拿到，籌碼分一直是 0、M3/M4 從來沒觸發 | 在 GitHub Actions 跑 `scripts/probe_sources.py`，印出 FinMind 原始回應 | 改用 `TaiwanStockInstitutionalInvestorsBuySell`；FinMind 失敗原因同時印到 stderr |
| 2026-10-17 | 就算拿到資料，法人名稱也是用中文比對，但 FinMind 回英文代號（實測：`Foreign_Investor`、`Foreign_Dealer_Self`、`Investment_Trust`、`Dealer_self`、`Dealer_Hedging`） | 翻 Actions log：所有入選股的訊號都沒有 M3/M4 | 中英文都認得；認不得的名稱寫進報告（`INSTITUTION_ALIASES`） |
| 2026-10-17 | 外資自營商如果加進外資會重複計算（證交所註明已含在自營商） | 參考 iThome 文章作者的經驗 | 外資只算 `Foreign_Investor` |
| 2026-10-17 | RR 篩選形同虛設：停損 -7%、目標至少 +15%，RR 最低 2.14 | log 裡入選股 RR 幾乎都是 2.1 | 停損改用 MA20、目標用 ATR（docs/SPEC.md） |
| 2026-10-17 | 股票池代號錯：2325 矽品早已下市、6247/6288 抓不到資料、3703 是欣陸不是欣興、6456/6530 都標成創意但創意是 3443 | log 每天「無資料」、收盤價和公司對不上 | 修正代號，名稱改用官方清單，`verify.py universe` 可檢查 |
| 2026-10-17 | 資料抓不到時默默變成 0 分或空清單，報告看起來正常 | 對照文章「綠燈不代表成功」 | 報告加「資料檢查」區塊，列出缺少的價格、法人、新聞 |

## 排程與執行

| 日期 | 坑 | 怎麼發現 | 處理 |
|---|---|---|---|
| 2026-10-17 | GitHub Actions 排程延遲 4.5～10 小時（設 17:30，實際 22:00～03:40） | 翻 Actions 執行紀錄 | 改由 Routine 17:53 執行；Actions 改成 21:07 的備援 |
| 2026-10-17 | 延遲 + 手動觸發造成同一天收到兩份日報（10/12、10/14、10/16） | 同上 | 用 `sent.json` 去重，同一個日期只發一次 |
| 2026-10-17 | 休市日（10/9 國慶補假）照樣把前一天的報告再發一次 | log 顯示 10/9 發了 10/8 的報告 | 平日沒有今日資料只發一行通知 |
| 2026-10-17 | 日報用 UTC 日期、辯證用台北日期，延遲到半夜時兩者不一致 | 10/9 的辯證剛好因為台北已經是 10/10 才跳過 | 一律用台北時間 |
| 2026-10-17 | 辯證 workflow 把整個選股流程重跑一次，兩份報告的 Top 5 可能不一樣，FinMind 請求也加倍 | 讀程式 | 合併成一個流程，辯證讀同一份 `picks.json` |
| 2026-10-17 | 每日結果只放在 Actions artifact，30 天就過期，沒辦法追蹤績效 | 讀 workflow | 存到 `claude/report-history` 分支 |
| 2026-10-17 | 抓歷史分支失敗時被當成「還沒有歷史」，會重發；推送衝突時 rebase 停在一半，下一次 push 沒東西可推卻回報成功 | 子代理審查 | 先用 `ls-remote` 分辨「不存在」和「連不上」；rebase 失敗就 abort；推完確認遠端是本機的 commit |
| 2026-10-17 | 備援排程延遲過午夜，「今天」變成隔天，誤發休市通知、漏掉當天報告 | 子代理審查 | 備援用台北時間減 6 小時當日期；17:00 前不判定休市 |
| 2026-10-17 | 17:53 法人資料還沒更新也照發並標成已發，備援就不會補 | 子代理審查 | 先等法人資料；還是缺就標成不完整，資料齊了再補發完整版 |
| 2026-10-17 | `ai_output.json` 格式壞掉（多包程式碼框、少逗號、型別錯）會讓 finalize 當掉，整份報告發不出去 | 子代理審查 | 容許程式碼框；讀檔和檢查都包 try，壞掉就只送程式數據 |
| 2026-10-17 | Python 的 `round()` 是銀行家捨入（2.5 → 2），會把正確的四捨五入判成「數字不在資料裡」；正負號和中文數字也沒檢查 | 子代理審查 | 改用誤差範圍比對；檢查漲跌方向；不允許中文數字 + 單位 |
| 2026-10-17 | 新聞時間用主機時區（UTC）顯示；證交所收盤價核對沒看資料日期 | 子代理審查 | 一律台北時間；證交所資料日期不是報告日期就不核對 |

## 雲端環境（Routine）

| 日期 | 坑 | 處理 |
|---|---|---|
| 2026-10-17 | 證交所請求寫死 `verify=certifi.where()`，雲端主機走代理、CA 由 `REQUESTS_CA_BUNDLE` 指定，寫死會 TLS 失敗 | 拿掉，用 requests 預設（會讀環境變數） |
| 2026-10-17 | 雲端主機的 `python3` 可能是沒裝套件的 3.11（文章坑 3），預裝的 3.13 是 pandas 3 | Routine 一律用 `/usr/bin/python3.13`；程式改成 pandas 2、3 都能跑，CI 兩個版本都測 |
| 2026-10-17 | 預設網路連不到 FinMind、證交所、Telegram | docs/ROUTINE.md 列出要加的網域 |
| 2026-10-18 | 雲端環境的金鑰和網路白名單只能由人在設定頁面填，Claude 沒有工具可以改 | 金鑰只放 GitHub Secrets，由 Actions 執行 |
| 2026-10-18 | 用工具（create_trigger）建立的 Routine 沒有掛 repo，session 裡沒有 GitHub 工具、`GH_TOKEN` 也無效，觸發不了 Actions | 實測發現；要在 Routine 設定裡選 repo |
| 2026-10-18 | Routine 扣**訂閱用量**（`five_hour`），不扣雲端點數；手動建立的雲端 session 才扣點數（`ccr_promotional`） | 用 get_session 看 rate_limit_info 實測。為了兩邊都省，每日報告改成只用 GitHub Actions |
| 2026-10-18 | GitHub 排程延遲好幾個小時 | 一天排 5 次、避開整點半點；用 sent.json 去重，多跑的那幾次一分鐘內結束；21:07 前資料不完整先不發，避免一天兩份 |
| 2026-10-18 | 開發用的雲端 session 連不到外部資料源，沒辦法驗證資料格式 | 改在 GitHub Actions 跑「資料源檢查」（`.github/workflows/probe.yml`），用 MCP 讀 log |
| 2026-10-18 | 資料源檢查用 `cmd \| tee` 記錄輸出，沒有 `set -o pipefail`，檢查失敗也回報通過 | 加上 pipefail |
| 2026-10-17 | 證交所擋雲端主機時回 HTTP 200 擋頁或沒有目標的 307（文章坑 1） | `_twse_get_json` 檢查內容，被擋寫進報告 |
| 2026-10-17 | 測試的 git 指令預設工作目錄寫成 `cwd=REPO_DIR` 預設參數，替換 `REPO_DIR` 沒用，結果在真正的 repo 執行，把一筆測試紀錄推到了 GitHub 的 `claude/report-history` | `_git` 改成呼叫時才決定目錄；測試先確認 git 指令跑在暫存 repo |

## API 與金鑰

| 日期 | 坑 | 處理 |
|---|---|---|
| 2026-10 | `ANTHROPIC_API_KEY` 尾巴帶換行，造成 InvalidHeader | 讀環境變數一律 `.strip()` |
| 2026-10 | 模型 ID 改了三次（舊的別名失效回 404） | 模型集中在 `config.CLAUDE_MODEL`，可用環境變數覆寫 |
| 2026-10 | anthropic SDK 在 Actions 上連線錯誤 | 直接用 requests 呼叫 Messages API |
| 2026-10 | AI 回的 JSON 包在 Markdown 程式碼框裡，用字串切割很容易壞 | API 備援改用結構化輸出（`output_config.format`） |
| 2026-10-17 | 測試用的假 Telegram token 太像真的會被 secret scanner 標紅（文章坑 6） | 假 token 寫成 `fake-bot-token-for-tests-only` |
| 2026-10-17 | Telegram token 在網址裡，例外訊息可能把 token 印出來 | 發送失敗只記例外類型 |
