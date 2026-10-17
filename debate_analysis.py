# debate_analysis.py - 舊入口，保留相容：等同 `python run_daily.py all --ai api`
#
# 多空辯證已經併進每日報告（第二則訊息）。原本這支會把整個選股流程重跑一次，
# 現在改成讀同一份 picks.json，數字由程式算好，AI 只寫文字（docs/AI_WRITING_RULES.md）。

import sys

from run_daily import main

if __name__ == "__main__":
    extra = ["--dry-run"] if "--dry-run" in sys.argv else []
    sys.exit(main(["all", "--ai", "api", *extra]))
