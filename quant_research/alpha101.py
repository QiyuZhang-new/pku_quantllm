"""101 Formulaic Alphas 的显式子集，来源 arXiv:1601.00991v3 Appendix A。

价格使用raw*cumadj，不做全样本归一化；rank为当日历史eligible内平均百分位秩。
所有信号在收盘后可知，统一次日开盘执行。未用成交额/成交笔数冒充volume。
"""


SOURCE = "https://arxiv.org/abs/1601.00991v3"
DEFINITIONS = {
    4: {"formula": "-Ts_Rank(rank(low), 9)", "fields": ["low"], "theme": "低价截面排名的时序反转"},
    9: {"formula": "d=delta(close,1); (ts_min(d,5)>0 or ts_max(d,5)<0) ? d : -d", "fields": ["close"], "theme": "连续走势与反转"},
    10: {"formula": "d=delta(close,1); rank((ts_min(d,4)>0 or ts_max(d,4)<0) ? d : -d)", "fields": ["close"], "theme": "连续走势的横截面排序"},
    18: {"formula": "-rank(stddev(abs(close-open),5) + (close-open) + correlation(close,open,10))", "fields": ["close", "open"], "theme": "日内变化、波动与开收盘相关"},
    20: {"formula": "-rank(open-delay(high,1))*rank(open-delay(close,1))*rank(open-delay(low,1))", "fields": ["open", "high", "low", "close"], "theme": "开盘相对昨日价格区间的偏离"},
    23: {"formula": "mean(high,20)<high ? -delta(high,2) : 0", "fields": ["high"], "theme": "高点突破后的反转"},
    33: {"formula": "rank(-(1-open/close))", "fields": ["open", "close"], "theme": "开收盘相对位置排序"},
    101: {"formula": "(close-open)/((high-low)+0.001)", "fields": ["open", "high", "low", "close"], "theme": "日内动量相对振幅"},
}


def catalog():
    known_blocks = {2: "需要真实成交量volume", 5: "需要VWAP", 48: "需要历史行业分类",
                    54: "原文为delay-0；未接入本系统的次日开盘协议", 56: "需要市值cap"}
    return [{"number": number, "implemented": number in DEFINITIONS,
             "formula": DEFINITIONS.get(number, {}).get("formula"),
             "fields": DEFINITIONS.get(number, {}).get("fields", []),
             "theme": DEFINITIONS.get(number, {}).get("theme", ""),
             "status": "已接入" if number in DEFINITIONS else known_blocks.get(number, "尚未实现/核验，不参加搜索"),
             "source": SOURCE} for number in range(1, 102)]


def compute(number, market):
    definition = DEFINITIONS[number]
    missing = set(definition["fields"]) - market.frames.keys()
    if missing:
        raise ValueError(f"Alpha#{number} 缺少字段 {sorted(missing)}")
    prices = {field: market.adjusted(field) for field in definition["fields"]}
    eligible = market.frames.get("eligible")

    def rank(frame):
        if eligible is not None:
            frame = frame.where(eligible.eq(1))
        return frame.rank(axis=1, pct=True, method="average")

    if number == 4:
        return -rank(prices["low"]).rolling(9, min_periods=9).rank(pct=True, method="average")
    if number in {9, 10}:
        change = prices["close"].diff()
        window = 5 if number == 9 else 4
        lowest = change.rolling(window, min_periods=window).min()
        highest = change.rolling(window, min_periods=window).max()
        result = change.where(lowest.gt(0) | highest.lt(0), -change).where(lowest.notna() & highest.notna())
        return result if number == 9 else rank(result)
    if number == 18:
        gap = prices["close"] - prices["open"]
        result = gap.abs().rolling(5, min_periods=5).std() + gap
        result += prices["close"].rolling(10, min_periods=10).corr(prices["open"])
        return -rank(result)
    if number == 20:
        return (-rank(prices["open"] - prices["high"].shift(1))
                * rank(prices["open"] - prices["close"].shift(1))
                * rank(prices["open"] - prices["low"].shift(1)))
    if number == 23:
        high = prices["high"]
        mean = high.rolling(20, min_periods=20).mean()
        return (-high.diff(2)).where(high.gt(mean), 0.).where(mean.notna() & high.diff(2).notna())
    if number == 33:
        return rank(-(1 - prices["open"] / prices["close"]))
    return (prices["close"] - prices["open"]) / (prices["high"] - prices["low"] + .001)
