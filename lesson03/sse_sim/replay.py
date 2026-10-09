"""Parquet 批次回放、通道业务序列排序、历史剩余订单簿。"""
from collections import Counter
from itertools import groupby
import heapq
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from .rules import Instrument, Quote, cents, feed_time, parse_time


REQUIRED = {
    "ord": {"Symbol", "Time", "Channel", "BizIndex", "OrderOriNo", "OrderKind", "FunctionCode", "Price", "Volume"},
    "exe": {"Symbol", "Time", "Channel", "BizIndex", "BidOrder", "AskOrder", "BSFlag", "Price", "Volume"},
    "snp": {"Symbol", "Time", "PreClose", "Price"} | {f"{side}{field}{n}" for side in ["Bid", "Ask"] for field in ["Price", "Volume"] for n in range(1, 11)},
}


def instruments_from_snapshots(directory, symbols):
    path = Path(directory)/"snp_20260923.parquet"
    found = {}
    for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=["Symbol", "Time", "PreClose"]):
        for row in batch.to_pylist():
            symbol = row["Symbol"]
            if symbol in symbols and symbol not in found and row["PreClose"] > 0:
                if feed_time(row["Time"]) >= parse_time("09:15:00"):
                    raise ValueError("缺少开盘前的前收盘价，不能使用未来静态信息")
                found[symbol] = Instrument(symbol, cents(row["PreClose"], feed=True),
                                           "STAR" if str(symbol).startswith("688") else "MAIN")
        if set(found) == set(symbols):
            return found
    raise ValueError("数据中找不到所选证券的开盘前信息")


def rows(directory, kind, symbols, end):
    file = pq.ParquetFile(Path(directory)/f"{kind}_20260923.parquet")
    missing = REQUIRED[kind]-set(file.schema_arrow.names)
    if missing:
        raise ValueError(f"{kind} 缺少字段: {sorted(missing)}")
    previous = -1
    for batch in file.iter_batches(batch_size=32768, columns=sorted(REQUIRED[kind])):
        if symbols:
            batch = batch.filter(pc.is_in(batch.column(batch.schema.get_field_index("Symbol")), value_set=pa.array(symbols, type=pa.int32())))
        for row in batch.to_pylist():
            original = feed_time(row["Time"])
            if original > end:
                return
            # 集合竞价期间的逐笔在竞价结束后才统一发布；原始订单时间不等于可观察时间。
            available = max(original, parse_time("09:25:00")) if kind != "snp" else original
            if available > end:
                return
            if available < previous:
                raise ValueError(f"{kind} 文件时间倒退")
            previous = available
            yield {"kind": kind, "time_ms": available, "original_time_ms": original, "data": row}


def merged_events(directory, symbols, end):
    streams = [rows(directory, kind, symbols, end) for kind in ["ord", "exe", "snp"]]
    merged = heapq.merge(*streams, key=lambda e: e["time_ms"])
    for _, bucket in groupby(merged, key=lambda e: e["time_ms"]):
        # 同一通道的逐笔以 BizIndex 排序；跨通道只有确定性顺序，没有虚构的全局先后。
        events = sorted(bucket, key=lambda e: (e["kind"] == "snp", e["data"].get("Channel", 0),
                                              e["data"].get("BizIndex", 0), e["data"]["Symbol"]))
        yield from events


def quote_from_snapshot(row):
    def levels(side):
        result = []
        for n in range(1, 11):
            p, v = row[f"{side}Price{n}"], row[f"{side}Volume{n}"]
            if p > 0 and v > 0:
                if int(v) != v:
                    raise ValueError("股数应为整数；不能擅自按手换算")
                result.append((cents(p, feed=True), int(v)))
        expected = sorted(result, reverse=side == "Bid")
        if result != expected or len({p for p, _ in result}) != len(result):
            raise ValueError("快照价位未按档位排序或有重复价位")
        return result
    bids, asks = levels("Bid"), levels("Ask")
    return Quote(bids, asks, cents(row["Price"], feed=True) if row["Price"] > 0 else 0, feed_time(row["Time"]))


class HistoricalBook:
    """独立于模拟成交的真实历史剩余量缓存。"""
    def __init__(self, symbol):
        self.symbol = symbol
        self.orders = {}
        self.totals = {"BUY": {}, "SELL": {}}
        self.stats = Counter()

    def _change(self, side, price, delta):
        levels = self.totals[side]
        value = levels.get(price, 0)+delta
        if value < 0:
            raise AssertionError("历史价位数量为负")
        if value:
            levels[price] = value
        else:
            levels.pop(price, None)

    def on_order(self, row):
        kind, flag = row["OrderKind"], row["FunctionCode"]
        if kind == ord("S"):
            self.stats[f"status_{chr(flag)}"] += 1
            return
        if kind not in {ord("A"), ord("D")} or flag not in {ord("B"), ord("S")}:
            raise ValueError("未知逐笔委托编码，停止而不是猜测")
        key = (row["Channel"], row["OrderOriNo"])
        if row["OrderOriNo"] <= 0:
            self.stats["missing_original_id"] += 1
            return
        side = "BUY" if flag == ord("B") else "SELL"
        quantity = row["Volume"]
        if kind == ord("A"):
            if quantity <= 0 or row["Price"] <= 0:
                self.stats["invalid_add"] += 1
                return
            if key in self.orders:
                self.stats["duplicate_add"] += 1
                return
            price = cents(row["Price"], feed=True)
            self.orders[key] = [side, price, quantity]
            self._change(side, price, quantity)
            self.stats["add"] += 1
        else:
            order = self.orders.get(key)
            if order is None:
                self.stats["unresolved_cancel"] += 1
                return
            if order[0] != side:
                raise ValueError("撤单方向与原订单不符")
            if quantity > order[2]:
                self.stats["cancel_overrun"] += 1
            self._consume(key, min(quantity, order[2]) if quantity > 0 else order[2])
            self.stats["cancel"] += 1

    def _consume(self, key, volume):
        order = self.orders[key]
        self._change(order[0], order[1], -volume)
        order[2] -= volume
        if not order[2]:
            del self.orders[key]

    def on_trade(self, row):
        self.stats["trade"] += 1
        for side, field in [("BUY", "BidOrder"), ("SELL", "AskOrder")]:
            key = (row["Channel"], row[field])
            order = self.orders.get(key)
            aggressive = row["BSFlag"] == (ord("B") if side == "BUY" else ord("S"))
            if order is None:
                # 首次主动成交前通常还没发布剩余新增委托，这是协议定义的正常情况。
                self.stats["unpublished_aggressor" if aggressive else "unresolved_passive"] += 1
                continue
            if order[0] != side:
                raise ValueError("成交关联原订单方向不符")
            if row["Volume"] > order[2]:
                self.stats["trade_overrun"] += 1
            self._consume(key, min(row["Volume"], order[2]))

    def levels(self, side, depth=10):
        return sorted(self.totals[side].items(), reverse=side == "BUY")[:depth]

    def check(self):
        expected = {"BUY": Counter(), "SELL": Counter()}
        for side, price, quantity in self.orders.values():
            assert quantity > 0
            expected[side][price] += quantity
        assert self.totals == {s: dict(v) for s, v in expected.items()}
