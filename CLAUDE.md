# taiwan-stock-report

台股盤後選股日報：程式抓資料、算好所有數字，AI 只寫文字，送到 Telegram。

- **如果你是 Routine 排程的 session**：照 `docs/ROUTINE.md` 的「給 Routine session 的步驟」執行，不要修改程式碼。
- 選股規則：`docs/SPEC.md`；AI 撰寫規則：`docs/AI_WRITING_RULES.md`；踩坑紀錄：`docs/PITFALLS.md`（遇到新坑就加一列）。

## 開發

- 入口是 `run_daily.py`（`main.py`、`debate_analysis.py` 只是相容用的包裝）。
- 測試：`python3.13 -m pytest -q`。改了程式要跑測試；CI 會在 Python 3.11 + pandas 2 和 Python 3.13 + pandas 3 兩種環境跑。
- 測試不能連網路，也不能碰真正的 git remote（`tests/test_history.py` 用本機 bare repo）。
- 數字一律由程式算；不要讓 AI 產生價位或百分比。新增給 AI 的欄位要放進 `ai_input.json`，`ai_validation.py` 才會允許 AI 引用。
- 抓不到的資料寫進 `DataQuality`（報告的「資料檢查」），不要默默補 0。
- 本機看報告不發送：`python3.13 run_daily.py all --dry-run --no-git`。
