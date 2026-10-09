"""规则控制的单证券撮合会话：开盘/收盘集合竞价及GFD生命周期。"""
from .matching import MatchingEngine
from .rules import Rules, Quote, Reject, Phase, phase_at, parse_time, cancellation_allowed


class Exchange:
    """不承担券商账户职责；申报的可卖量/权限由券商层提供。"""
    def __init__(self, instrument):
        self.instrument = instrument
        self.book = MatchingEngine()
        self.rules = Rules()
        self.now = -1
        self.last_price = 0
        self.opened = False
        self.closed = False
        self.trades = []
        self.expired = []

    def _record(self, trades):
        self.trades.extend(trades)
        if trades:
            self.last_price = trades[-1].price

    def advance(self, now):
        if now < self.now:
            raise Reject("撮合会话时间不能倒退")
        self.now = now
        if now >= parse_time("09:25:00") and not self.opened:
            self._record(self.book.auction())
            self.opened = True
        if now >= parse_time("15:00:00") and not self.closed:
            self._record(self.book.auction())
            self.closed = True
            for order in self.book.orders.values():
                if order.remaining:
                    self.expired.append(order.order_id)
                    self.book.cancel(order.order_id)

    def submit(self, order_id, side, price, quantity, now, *, sellable=0, star_permission=False):
        self.advance(now)
        quote = Quote(self.book.levels("BUY"), self.book.levels("SELL"), self.last_price, now)
        self.rules.validate(self.instrument, side, price, quantity, now, quote,
                            sellable=sellable, star_permission=star_permission)
        trades = self.book.add(order_id, side, price, quantity,
                               continuous=phase_at(now) == Phase.CONTINUOUS)
        self._record(trades)
        return trades

    def cancel(self, order_id, now):
        self.advance(now)
        if not cancellation_allowed(now):
            raise Reject("该时段交易所不接受撤单")
        if order_id not in self.book.orders or not self.book.orders[order_id].remaining:
            raise Reject("订单没有可撤剩余量")
        return self.book.cancel(order_id)
