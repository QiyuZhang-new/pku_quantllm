"""规则与金额处理：价格/资金使用整数分，时间使用当日毫秒。"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


class Reject(ValueError):
    pass


class Phase(str, Enum):
    CLOSED = "闭市"
    OPEN_CALL = "开盘集合竞价"
    BREAK = "非申报时段"
    CONTINUOUS = "连续竞价"
    CLOSE_CALL = "收盘集合竞价"
    POST = "盘后固定价格交易"


def parse_time(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("时间应为 HH:MM:SS[.mmm]")
    h, m = map(int, parts[:2])
    seconds = Decimal(parts[2])
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= seconds < 60):
        raise ValueError("时间超出范围")
    millis = seconds * 1000
    if millis != millis.to_integral_value():
        raise ValueError("最多支持毫秒精度")
    return (h * 3600 + m * 60) * 1000 + int(millis)


def feed_time(raw: int) -> int:
    raw = int(raw)
    h, rem = divmod(raw, 10000000)
    m, rem = divmod(rem, 100000)
    s, ms = divmod(rem, 1000)
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"行情时间格式错误: {raw}")
    return (h * 3600 + m * 60 + s) * 1000 + ms


def format_time(ms: int) -> str:
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, rem = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02}.{rem:03}"


def cents(value, *, feed=False) -> int:
    """用户价格必须精确符合 0.01；仅行情 float32 允许舍入误差。"""
    d = Decimal(str(value))
    if not d.is_finite() or d <= 0:
        raise Reject("价格必须为正的有限数")
    scaled = d * 100
    rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if not feed and scaled != rounded:
        raise Reject("A股报价最小变动为0.01元")
    if feed and abs(scaled - rounded) > Decimal("0.05"):
        raise Reject("行情价格不符合A股价格档位")
    return int(rounded)


def rounded(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def phase_at(t: int) -> Phase:
    if parse_time("09:15:00") <= t < parse_time("09:25:00"):
        return Phase.OPEN_CALL
    if (parse_time("09:30:00") <= t <= parse_time("11:30:00")
            or parse_time("13:00:00") <= t < parse_time("14:57:00")):
        return Phase.CONTINUOUS
    if parse_time("14:57:00") <= t < parse_time("15:00:00"):
        return Phase.CLOSE_CALL
    if parse_time("15:05:00") <= t <= parse_time("15:30:00"):
        return Phase.POST
    if parse_time("09:25:00") <= t < parse_time("15:05:00"):
        return Phase.BREAK
    return Phase.CLOSED


def cancellation_allowed(t: int) -> bool:
    p = phase_at(t)
    return p == Phase.CONTINUOUS or (p == Phase.OPEN_CALL and t < parse_time("09:20:00"))


@dataclass(frozen=True)
class Instrument:
    symbol: int
    previous_close: int
    board: str = "MAIN"
    # 本数据缺少上市天数/停牌等静态信息；演示显式采用常规限价股。
    ordinary: bool = True
    suspended: bool = False

    def __post_init__(self):
        if self.previous_close <= 0 or self.board not in {"MAIN", "STAR"}:
            raise ValueError("证券参数无效")

    @property
    def lot(self):
        return 200 if self.board == "STAR" else 100

    @property
    def limits(self):
        rate = Decimal("0.20" if self.board == "STAR" else "0.10")
        p = Decimal(self.previous_close)
        return max(1, rounded(p * (1-rate))), max(self.previous_close+1, rounded(p*(1+rate)))


@dataclass
class Quote:
    bids: list[tuple[int, int]]
    asks: list[tuple[int, int]]
    last: int = 0
    time: int = 0

    def base(self, side, previous_close):
        first, fallback = (self.asks, self.bids) if side == "BUY" else (self.bids, self.asks)
        return first[0][0] if first else (fallback[0][0] if fallback else (self.last or previous_close))


class Rules:
    """仅接收普通主板/科创板 A股限价订单；不支持的类型明确拒绝。"""
    def validate(self, instrument, side, price, quantity, now, quote=None,
                 *, sellable=0, star_permission=False, order_type="LIMIT"):
        if order_type != "LIMIT":
            raise Reject("此教学系统仅实现限价申报")
        if not instrument.ordinary or instrument.suspended:
            raise Reject("当前证券特殊状态不在已支持范围")
        phase = phase_at(now)
        if phase not in {Phase.OPEN_CALL, Phase.CONTINUOUS, Phase.CLOSE_CALL}:
            raise Reject("当前时段不接受竞价申报")
        if side not in {"BUY", "SELL"}:
            raise Reject("买卖方向无效")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise Reject("数量必须为正整数股")
        if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
            raise Reject("价格必须为整数分")
        maximum = 100000 if instrument.board == "STAR" else 1000000
        if quantity > maximum:
            raise Reject("超过该板块单笔限价申报上限")
        if instrument.board == "STAR" and side == "BUY" and not star_permission:
            raise Reject("未开通科创板权限/风险揭示书授权")
        if side == "BUY":
            if quantity < instrument.lot or (instrument.board == "MAIN" and quantity % 100):
                raise Reject("买入数量不符合板块申报单位")
        else:
            if quantity > sellable:
                raise Reject("可卖持仓不足（含T+1/已冻结持仓）")
            # 零股可以连同整手一次清仓，不允许拆开零股卖出。
            odd = quantity < instrument.lot if instrument.board == "STAR" else quantity % 100 != 0
            complete_odd_balance = (quantity == sellable if instrument.board == "STAR"
                                    else quantity % 100 == sellable % 100)
            if odd and not complete_odd_balance:
                raise Reject("不足申报单位的余额必须一次性卖出")
        low, high = instrument.limits
        if not low <= price <= high:
            raise Reject("价格超过涨跌停范围")
        if phase == Phase.CONTINUOUS:
            q = quote or Quote([], [])
            base = q.base(side, instrument.previous_close)
            if side == "BUY":
                upper = rounded(Decimal(base)*Decimal("1.02"))
                if instrument.board == "MAIN":
                    upper = max(upper, base+10)
                upper = max(upper, base+1)
                if price > upper:
                    raise Reject("买入价格超出连续竞价价格笼子")
            else:
                lower = rounded(Decimal(base)*Decimal("0.98"))
                if instrument.board == "MAIN":
                    lower = min(lower, base-10)
                lower = max(1, min(lower, base-1))
                if price < lower:
                    raise Reject("卖出价格超出连续竞价价格笼子")
