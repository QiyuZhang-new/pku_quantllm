"""下载公开历史 ST 字段，逐证券缓存，断点续跑；不使用账号凭据。"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import socket
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "data"


def initialize():
    import baostock as bs
    socket.setdefaulttimeout(20)
    bs.login()


def alarm_handler(signum, frame):
    raise TimeoutError("公开行情请求超过30秒")


def fetch_symbol(symbol):
    import baostock as bs
    path = OUT / "historical_status" / f"{symbol}.feather"
    if path.exists():
        return symbol, "cached", len(pd.read_feather(path))
    code = symbol[-2:].lower() + "." + symbol[:6]
    signal.signal(signal.SIGALRM, alarm_handler)
    for attempt in range(3):
        try:
            signal.alarm(30)
            rs = bs.query_history_k_data_plus(
                code, "date,code,isST,tradestatus,preclose",
                start_date="2022-01-01", end_date="2026-09-30", frequency="d", adjustflag="3",
            )
            if rs.error_code != "0":
                raise ValueError(f"{rs.error_code}: {rs.error_msg}")
            # 每证券区间小于2000行；显式迭代兼容 pandas 3，保留分页能力。
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                raise ValueError(f"读取分页失败 {rs.error_code}: {rs.error_msg}")
            frame = pd.DataFrame(rows, columns=rs.fields)
            if not frame.empty:
                temporary = path.with_suffix(".part")
                frame.to_feather(temporary)
                temporary.replace(path)
            signal.alarm(0)
            return symbol, "ok" if len(frame) else "empty", len(frame)
        except Exception as exc:
            signal.alarm(0)
            if attempt == 2:
                return symbol, f"error: {type(exc).__name__}: {exc}", 0
            time.sleep(1)
            bs.login()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("并行连接仅允许1至4个，避免过度请求公共服务")
    (OUT / "historical_status").mkdir(parents=True, exist_ok=True)
    import pyarrow.feather as feather
    symbols = feather.read_table(ROOT / "feathers/close.feather").column_names[1:]
    if args.limit:
        symbols = symbols[:args.limit]
    records = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=initialize) as executor:
        futures = {executor.submit(fetch_symbol, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol, status, count = future.result()
            records.append({"symbol": symbol, "status": status, "rows": count})
            if len(records) % 100 == 0 or status.startswith("error"):
                print(f"{len(records)}/{len(symbols)} {symbol} {status}, {time.monotonic()-started:.1f}s", flush=True)
    pd.DataFrame(records).sort_values("symbol").to_csv(OUT / "status_download_log.csv", index=False)
    metadata = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "BaoStock", "api": "query_history_k_data_plus",
        "fields": "date,code,isST,tradestatus,preclose", "start": "2022-01-01", "end": "2026-09-30",
        "symbols": len(records), "failed": sum(r["status"].startswith("error") for r in records),
        "empty": sum(r["status"] == "empty" for r in records)}
    (OUT / "status_download_manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(json.dumps(metadata, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
