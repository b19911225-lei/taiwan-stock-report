@echo off
chcp 65001 >nul
REM 司法院裁判書批次檢索 — 元德寶宮 v. 富邦人壽 年金保價金受益人爭點
REM 先確認已安裝 Python 與 requests：pip install requests

setlocal
set PY=python
set OUT=judgments

%PY% fjud_search.py --kw "年金保單價值準備金 and 受益人"                         --out %OUT%\01_保價金受益人
%PY% fjud_search.py --kw "未支領之年金餘額 and 繼承人"                           --out %OUT%\02_年金餘額繼承人
%PY% fjud_search.py --kw "年金給付開始日 and 身故受益人 and 保單價值準備金"      --out %OUT%\03_給付開始日
%PY% fjud_search.py --kw "遞延年金 and 受益人 and 遺產"                          --out %OUT%\04_遞延年金遺產
%PY% fjud_search.py --kw "保險金 and 非受益人 and 清償效力"                      --out %OUT%\05_清償效力
%PY% fjud_search.py --kw "保險法第135條之3 and 受益人"                           --out %OUT%\06_135條之3

echo.
echo 完成。各子目錄下有 index.csv 與判決全文 txt。
pause
