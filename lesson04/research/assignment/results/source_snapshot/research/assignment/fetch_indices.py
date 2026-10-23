"""缓存腾讯指数日线及BaoStock独立校验数据，严格检查区间覆盖。"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import socket
from urllib.request import urlopen

import pandas as pd

from .fetch_data import alarm_handler


def main():
    import baostock as bs
    out = Path(__file__).resolve().parent / "data"
    out.mkdir(parents=True, exist_ok=True)
    socket.setdefaulttimeout(20)
    signal.signal(signal.SIGALRM, alarm_handler)
    bs.login()
    manifest = []
    for label, code in [("csi300", "sh000300"), ("sse", "sh000001")]:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
               f"{code},day,2024-12-01,2026-09-30,640,qfq")
        raw = urlopen(url, timeout=30).read()
        (out / f"{label}_tencent.json").write_bytes(raw)
        content = json.loads(raw)
        if content.get("code") != 0:
            raise ValueError(content)
        rows = content["data"][code]["day"]
        frame = pd.DataFrame([r[:6] for r in rows], columns=["date", "open", "close", "high", "low", "volume"])
        frame["date"] = pd.to_datetime(frame.date)
        frame = frame.set_index("date").astype(float)
        signal.alarm(30)
        rs = bs.query_history_k_data_plus(code[:2]+"."+code[2:], "date,open,close",
            start_date="2024-12-01", end_date="2026-09-30", frequency="d", adjustflag="3")
        if rs.error_code != "0":
            raise ValueError(f"指数交叉检查失败: {rs.error_code}, {rs.error_msg}")
        check = pd.DataFrame(rs.data, columns=rs.fields)
        signal.alarm(0)
        check.to_csv(out / f"{label}_baostock.csv", index=False)
        check["date"] = pd.to_datetime(check.date)
        check = check.set_index("date").astype(float)
        joined = frame[["open", "close"]].join(check, lsuffix="_qq", rsuffix="_bs", how="inner")
        error = max((joined.open_qq-joined.open_bs).abs().max(), (joined.close_qq-joined.close_bs).abs().max())
        if error > .02:
            raise ValueError(f"{label}两源指数差异过大 {error}")
        for endpoint in ["2025-01-02", "2026-09-30"]:
            if pd.Timestamp(endpoint) not in frame.index:
                raise ValueError(f"{label} 缺少作业端点 {endpoint}")
        frame.to_csv(out / f"{label}.csv")
        manifest.append({"label": label, "url": url, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "sha256_raw": hashlib.sha256(raw).hexdigest(), "rows": len(frame),
            "crosscheck_rows": len(joined), "crosscheck_max_abs_price_difference": float(error)})
    sample = bs.query_history_k_data_plus("sh.600000", "date,code,open,close,amount,isST",
        start_date="2025-01-02", end_date="2025-01-02", frequency="d", adjustflag="3")
    if sample.error_code != "0" or len(sample.data) != 1:
        raise ValueError("成交额单位校验样本获取失败")
    pd.DataFrame(sample.data, columns=sample.fields).to_feather(out / "sample_600000.feather")
    bs.logout()
    (out / "indices_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
