"""模拟券商账户：资源冻结、T+1、费用、委托及成交回报。"""
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal
from .rules import Reject, Rules, Quote, Instrument, Phase, phase_at, cancellation_allowed, rounded, parse_time


@dataclass
class Position:
    yesterday: int = 0
    today: int = 0
    frozen: int = 0

    @property
    def sellable(self):
        return self.yesterday-self.frozen


@dataclass
class Account:
    cash: int = 10000000
    frozen_cash: int = 0
    star_permission: bool = False
    positions: dict[int, Position] = field(default_factory=dict)

    @property
    def available_cash(self):
        return self.cash-self.frozen_cash

    def position(self, symbol):
        return self.positions.setdefault(symbol, Position())


@dataclass(frozen=True)
class Fees:
    # 佣金为演示假设，包含券商经手费等；可通过配置替换。
    commission: Decimal = Decimal("0.0003")
    minimum: int = 500
    transfer: Decimal = Decimal("0.00001")
    stamp_sell: Decimal = Decimal("0.0005")

    def total(self, amount: int, side: str):
        if amount <= 0:
            return 0
        commission = max(self.minimum, rounded(Decimal(amount)*self.commission))
        transfer = rounded(Decimal(amount)*self.transfer)
        stamp = rounded(Decimal(amount)*self.stamp_sell) if side == "SELL" else 0
        return commission+transfer+stamp


@dataclass
class Order:
    order_id: str
    symbol: int
    side: str
    price: int
    quantity: int
    remaining: int
    submitted: int
    eligible: int
    sequence: int
    status: str = "PENDING"
    notional: int = 0
    fee: int = 0
    reserved: int = 0
    filled: int = 0


class PaperBroker:
    """只对连续竞价的已可见快照模拟成交；不虚构集合竞价排队位置。"""
    active_states = {"PENDING", "ACCEPTED", "PARTIAL"}

    def __init__(self, instruments: dict[int, Instrument], account=None, fees=None, latency_ms=100):
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("延迟必须为非负整数毫秒")
        self.instruments = instruments
        self.account = account or Account()
        self.fees = fees or Fees()
        self.latency_ms = latency_ms
        self.rules = Rules()
        self.quotes = {}
        self.orders = {}
        self.events = []
        self.trades = []
        self.counter = 0
        self.last_time = -1
        self.quote_versions = {}
        self.trading_date = "2026-09-23"
        self.assert_invariants()

    def log(self, event, time, **data):
        self.events.append({"event": event, "time_ms": time, **data})

    def advance(self, now):
        """推进账户时钟；收盘失效不依赖所选证券有没有快照。"""
        if isinstance(now, bool) or not isinstance(now, int) or not 0 <= now < 86400000:
            raise Reject("时钟必须为当日整数毫秒")
        if now < self.last_time:
            raise Reject("账户时钟不能倒退")
        if self.last_time < parse_time("15:00:00") <= now:
            self.expire(parse_time("15:00:00"))
        self.last_time = now

    def submit(self, symbol, side, price, quantity, now, *, order_type="LIMIT"):
        self.counter += 1
        oid = f"SIM-{self.counter:06d}"
        try:
            self.advance(now)
            instrument = self.instruments.get(symbol)
            if instrument is None:
                raise Reject("证券未配置")
            quote = self.quotes.get(symbol)
            if quote is None or quote.time > now or now-quote.time > 3000:
                raise Reject("行情缺失、超时或来自未来")
            self.rules.validate(instrument, side, price, quantity, now, quote,
                                sellable=self.account.position(symbol).sellable,
                                star_permission=self.account.star_permission, order_type=order_type)
            if phase_at(now) != Phase.CONTINUOUS:
                raise Reject("历史回放缺少真实竞价排队信息；集合竞价请使用独立撮合内核")
            reserve = price*quantity+self.fees.total(price*quantity, side) if side == "BUY" else self.fees.minimum
            if self.account.available_cash < reserve:
                raise Reject("可用资金不足（含费用预留）")
        except Reject as exc:
            self.log("REJECTED", now, order_id=oid, symbol=symbol, side=side, reason=str(exc))
            return None
        order = Order(oid, symbol, side, price, quantity, quantity, now,
                      now+self.latency_ms, self.counter, reserved=reserve)
        self.orders[oid] = order
        self.account.frozen_cash += reserve
        if side == "SELL":
            self.account.position(symbol).frozen += quantity
        self.log("SUBMITTED", now, **asdict(order))
        self.assert_invariants()
        return oid

    def _release(self, order, state, now):
        self.account.frozen_cash -= order.reserved
        order.reserved = 0
        if order.side == "SELL":
            self.account.position(order.symbol).frozen -= order.remaining
        order.status = state
        self.log(state, now, order_id=order.order_id, symbol=order.symbol, remaining=order.remaining)

    def cancel(self, order_id, now):
        try:
            self.advance(now)
        except Reject as exc:
            self.log("CANCEL_REJECTED", now, order_id=order_id, reason=str(exc))
            return False
        order = self.orders.get(order_id)
        if not order or order.status not in self.active_states:
            self.log("CANCEL_REJECTED", now, order_id=order_id, reason="没有可撤的活动委托")
            return False
        if now < max(order.submitted, self.last_time):
            self.log("CANCEL_REJECTED", now, order_id=order_id, reason="撤单时钟不能倒退")
            return False
        if not cancellation_allowed(now):
            self.log("CANCEL_REJECTED", now, order_id=order_id, reason="该时段不接受撤单")
            return False
        self._release(order, "CANCELLED", now)
        self.assert_invariants()
        return True

    def expire(self, now):
        if now < max(self.last_time, parse_time("15:00:00")):
            raise Reject("只能在收盘后按顺序进行当日失效")
        self.last_time = now
        for order in self.orders.values():
            if order.status in self.active_states:
                self._release(order, "EXPIRED", now)
        self.assert_invariants()

    def on_quote(self, symbol, quote: Quote):
        instrument = self.instruments[symbol]
        low, high = instrument.limits
        for side, levels in [("BUY", quote.bids), ("SELL", quote.asks)]:
            if levels != sorted(levels, reverse=side == "BUY") or len({p for p, _ in levels}) != len(levels):
                raise ValueError("盘口价位顺序或唯一性错误")
            if any(not isinstance(p, int) or not isinstance(v, int) or not low <= p <= high or v <= 0 for p, v in levels):
                raise ValueError("盘口价格/数量不符合配置证券规则")
        if phase_at(quote.time) == Phase.CONTINUOUS and quote.bids and quote.asks and quote.bids[0][0] > quote.asks[0][0]:
            raise ValueError("连续竞价不接受交叉盘口")
        if quote.time < self.last_time:
            raise ValueError("回放时钟倒退")
        self.advance(quote.time)
        old = self.quote_versions.get(symbol, -1)
        if quote.time <= old:
            return  # 同一时刻不能重复获得相同外部流动性。
        self.quote_versions[symbol] = quote.time
        # 保存观察报价，与模拟消耗的外部深度分开。
        self.quotes[symbol] = Quote(list(quote.bids), list(quote.asks), quote.last, quote.time)
        if phase_at(quote.time) != Phase.CONTINUOUS:
            return
        bids, asks = [list(x) for x in quote.bids], [list(x) for x in quote.asks]
        eligible = [o for o in self.orders.values() if o.symbol == symbol
                    and o.status in self.active_states and o.eligible <= quote.time]
        # 外部快照没有对手方订单FIFO信息；自身订单遵守价格/时间优先。
        eligible.sort(key=lambda o: (o.side, -o.price if o.side == "BUY" else o.price, o.eligible, o.sequence))
        for order in eligible:
            if order.status == "PENDING":
                position = self.account.position(symbol)
                try:
                    self.rules.validate(self.instruments[symbol], order.side, order.price, order.quantity,
                                        quote.time, quote, sellable=position.sellable+(order.remaining if order.side == "SELL" else 0),
                                        star_permission=self.account.star_permission)
                except Reject as exc:
                    self._release(order, "REJECTED", quote.time)
                    self.events[-1]["reason"] = "到达时校验失败："+str(exc)
                    continue
                order.status = "ACCEPTED"
                self.log("ACCEPTED", quote.time, order_id=order.order_id, symbol=order.symbol)
            depth = asks if order.side == "BUY" else bids
            for level in depth:
                price, available = level
                crossing = price <= order.price if order.side == "BUY" else price >= order.price
                if not crossing:
                    break
                if not available or not order.remaining:
                    continue
                volume = min(available, order.remaining)
                level[1] -= volume
                self._fill(order, price, volume, quote.time)
        self.assert_invariants()

    def _fill(self, order, price, quantity, now):
        old_reserve = order.reserved
        amount = price*quantity
        order.notional += amount
        cumulative_fee = self.fees.total(order.notional, order.side)
        incremental_fee = cumulative_fee-order.fee
        order.fee = cumulative_fee
        order.remaining -= quantity
        order.filled += quantity
        position = self.account.position(order.symbol)
        if order.side == "BUY":
            self.account.cash -= amount+incremental_fee
            position.today += quantity
            worst = order.notional+order.price*order.remaining
            order.reserved = (order.price*order.remaining+self.fees.total(worst, "BUY")-order.fee) if order.remaining else 0
        else:
            self.account.cash += amount-incremental_fee
            position.yesterday -= quantity
            position.frozen -= quantity
            order.reserved = 0  # 最低佣金已扣；后续增量费用由卖出款覆盖。
        self.account.frozen_cash += order.reserved-old_reserve
        order.status = "PARTIAL" if order.remaining else "FILLED"
        trade = {"trade_id": f"FILL-{len(self.trades)+1:06d}", "order_id": order.order_id,
                 "symbol": order.symbol, "side": order.side, "time_ms": now,
                 "price_cents": price, "quantity": quantity, "fee_cents": incremental_fee,
                 "model": "next-visible-snapshot-depth"}
        self.trades.append(trade)
        self.log("FILL", now, **{k: v for k, v in trade.items() if k != "time_ms"})

    def assert_invariants(self):
        assert 0 <= self.account.frozen_cash <= self.account.cash
        assert self.account.frozen_cash == sum(o.reserved for o in self.orders.values())
        for symbol, p in self.account.positions.items():
            assert p.today >= 0 and 0 <= p.frozen <= p.yesterday
            assert p.frozen == sum(o.remaining for o in self.orders.values()
                                   if o.symbol == symbol and o.side == "SELL" and o.status in self.active_states)
        for order in self.orders.values():
            assert order.filled+order.remaining == order.quantity

    def settlement(self, next_date, current_date, *, trading_calendar):
        date.fromisoformat(current_date)
        date.fromisoformat(next_date)
        if current_date != self.trading_date or next_date <= current_date:
            raise ValueError("交收必须从当前交易日向后推进")
        future = sorted(d for d in trading_calendar if d > current_date)
        if not future or future[0] != next_date:
            raise ValueError("必须提供已核验的下一交易日，不能跳过交收日")
        if self.last_time < parse_time("15:00:00"):
            raise ValueError("当天竞价尚未收盘，不能提前解锁今日买入")
        self.expire(self.last_time)
        for position in self.account.positions.values():
            position.yesterday += position.today
            position.today = 0
        self.trading_date = next_date
        self.quotes.clear()
        self.quote_versions.clear()
        self.last_time = -1

    def state(self):
        return {"cash_cents": self.account.cash, "frozen_cash_cents": self.account.frozen_cash,
                "available_cash_cents": self.account.available_cash,
                "positions": {str(k): {**asdict(v), "sellable": v.sellable} for k, v in self.account.positions.items()},
                "orders": [asdict(o) for o in self.orders.values()]}
