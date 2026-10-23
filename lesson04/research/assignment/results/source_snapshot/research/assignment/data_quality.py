"""外部原始公告与行情样本交叉核对；不是对全部标签正确性的保证。"""

import json

import pandas as pd

from .run import BASE, DATA, digest


def main():
    local = float(pd.read_feather(BASE/"feathers/amt.feather", columns=["date", "600000.SH"])
                  .set_index("date").loc["2025-01-02", "600000.SH"])
    external = float(pd.read_feather(DATA/"sample_600000.feather").amount.iloc[0])
    history = pd.read_feather(DATA/"historical_status/001270.SZ.feather").set_index("date")
    cases = [("2025-04-23", 0, 0), ("2025-04-24", 1, 1), ("2026-05-19", 1, 0), ("2026-05-20", 0, 1)]
    rows = []
    for date, st, status in cases:
        actual_st = int(history.at[date, "isST"])
        actual_status = int(history.at[date, "tradestatus"])
        rows.append({"symbol": "001270.SZ", "date": date, "is_st": actual_st,
            "trade_status": actual_status, "expected_st": st, "expected_trade_status": status,
            "matches": bool(actual_st == st and actual_status == status)})
    result = {"amount_unit_sample": {"date": "2025-01-02", "symbol": "600000.SH", "local_amt": local,
        "baostock_amount": external, "unit": "人民币元", "matches": bool(abs(local-external)<.01)},
        "st_announcement_crosschecks": rows,
        "announcement_sources": [
            "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-04-23/1fc0c56b-fbc2-43c1-bd4c-aca59b26b088.PDF",
            "https://disc.static.szse.cn/disc/disk03/finalpage/2026-05-19/7dcd926a-5a98-4b8b-b5cb-185bc679a713.PDF"],
        "source_sha256": digest(__file__)}
    result["passed"] = bool(result["amount_unit_sample"]["matches"] and all(r["matches"] for r in rows))
    (DATA/"data_crosschecks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise SystemExit("外部数据抽样校验失败")


if __name__ == "__main__":
    main()
