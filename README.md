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
| Claude Code Routine（主要） | 每個交易日 17:53 | 照 [docs/ROUTINE.md](docs/ROUTINE.md)；AI 文字由排程的 Claude 撰寫 |
| GitHub Actions（備援） | 每個交易日 21:07 | Routine 發過就跳過；沒發才補發，AI 文字改用 Claude API |
| 本機 | 隨時 | `python run_daily.py all --dry-run --no-git` 看報告不發送 |

需要的環境變數：`FINMIND_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`；
備援模式另外需要 `ANTHROPIC_API_KEY`（模型可用 `CLAUDE_MODEL` 覆寫）。

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
