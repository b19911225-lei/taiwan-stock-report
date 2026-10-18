# 台股選股日報

每個交易日盤後，從主題股（AI 伺服器、CPO、矽光子、HBM、散熱電源）和當日成交量前 10 名裡，
用技術面、量能、法人籌碼篩出觀察清單，附上進場／目標／停損與多空辯證，送到 Telegram。

**設計原則**（參考 iThome〈Claude Code 雲端實戰篇〉）：

- 數字由程式算，AI 只負責寫文字；AI 寫出的數字必須在資料裡找得到，否則該段落不會送出。
- 抓不到的資料寫在報告的「資料檢查」，不自己補。
- 另外寫核對程式：收盤價和證交所比對、報告上的數字和計算結果比對。
- 每天的結果存回 GitHub（`claude/report-history` 分支），用來去重與追蹤過去推薦的績效。

## 怎麼跑

| 方式 | 時間 | 說明 |
|---|---|---|
| GitHub Actions（主要） | 每個交易日 17:47、18:17、18:47、20:23、21:07 | 只用 GitHub 免費額度。第一個跑到的發報告，之後的看到已發過就跳過；21:07 前資料不完整先不發 |
| 準時觸發（雲端點數） | 交易日 17:52，到 2026-11-04 | 排程「台股選股日報 - 每日觸發（點數）」叫醒一個手動建立的雲端 session，只負責觸發上面的 workflow（GitHub 排程常延遲）。扣雲端點數，點數 11/5 到期前自動停用 |
| 手動補發 | 隨時 | GitHub → Actions →「台股選股日報」→ Run workflow，填日期（YYYY-MM-DD）並勾 force |
| Claude Code Routine | 目前停用 | 會扣訂閱用量；設計保留在 [docs/ROUTINE.md](docs/ROUTINE.md) |
| 本機 | 隨時 | `python run_daily.py all --dry-run --no-git` 看報告不發送 |

**用量優先順序**：GitHub Actions 免費額度 → 雲端點數 → 訂閱用量（目前不使用）。AI 文字用 `ANTHROPIC_API_KEY`，實測每天約 0.02 美元。

需要的 GitHub Secrets：`FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`ANTHROPIC_API_KEY`
（AI 文字的模型可用 `CLAUDE_MODEL` 覆寫，預設 `claude-sonnet-5-5`）。不需要任何 GitHub 個人存取權杖。

## 文件

- [docs/SPEC.md](docs/SPEC.md)：篩選、評分、進出場價位、績效追蹤的規則
- [docs/AI_WRITING_RULES.md](docs/AI_WRITING_RULES.md)：AI 撰寫規則與輸出格式
- [docs/ROUTINE.md](docs/ROUTINE.md)：Routine 步驟與第一次設定（網路白名單、環境變數）
- [docs/PITFALLS.md](docs/PITFALLS.md)：踩坑紀錄

## 檢查工具

```bash
python verify.py universe            # 股票池代號和官方名稱是否一致
python verify.py report 2026-10-16   # 重新核對某天報告上的數字
python -m pytest -q                  # 測試（需要 requirements-dev.txt）
```

> 本報告為程式與 AI 輔助整理，僅供參考，不構成投資建議。
