# main.py - 舊入口，保留相容：等同 `python run_daily.py all --ai none`
#
# 新流程請用 run_daily.py（說明見檔案開頭與 docs/ROUTINE.md）。

import sys

from run_daily import main

if __name__ == "__main__":
    extra = ["--dry-run"] if "--dry-run" in sys.argv else []
    sys.exit(main(["all", "--ai", "none", *extra]))
