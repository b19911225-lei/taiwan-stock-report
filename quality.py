# quality.py - 記錄「今日缺少」與資料檢查結果
#
# 文章的教訓：抓不到的資料要在報告上寫出來，不能默默變成 0 分或空清單；
# 綠燈（程式正常結束）不代表資料完整。

from dataclasses import dataclass, field


@dataclass
class DataQuality:
    missing_price: list = field(default_factory=list)       # 抓不到價格的股票代號
    missing_inst: list = field(default_factory=list)        # 抓不到（或還沒更新）法人資料的股票代號
    notes: list = field(default_factory=list)               # 其他缺少或異常（例如證交所擋、新聞失敗）
    checks: list = field(default_factory=list)              # 核對結果（例如收盤價和證交所一致）

    def add_note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def add_check(self, text):
        if text not in self.checks:
            self.checks.append(text)

    def to_dict(self):
        return {
            "missing_price": sorted(set(self.missing_price)),
            "missing_inst": sorted(set(self.missing_inst)),
            "notes": list(self.notes),
            "checks": list(self.checks),
        }

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            missing_price=list(d.get("missing_price", [])),
            missing_inst=list(d.get("missing_inst", [])),
            notes=list(d.get("notes", [])),
            checks=list(d.get("checks", [])),
        )

    def is_empty(self):
        return not (self.missing_price or self.missing_inst or self.notes)
