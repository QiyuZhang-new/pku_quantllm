"""独立交易所撮合内核；历史剩余委托不在这里再次撮合。"""
from dataclasses import dataclass
from decimal import Decimal
from .rules import rounded


@dataclass
class BookOrder:
    order_id: str
    side: str
    price: int
    remaining: int
    sequence: int


@dataclass(frozen=True)
class Match:
    buy_id: str
    sell_id: str
    price: int
    quantity: int


class MatchingEngine:
    """调用方先做申报/账户校验；同价按内核接收序号排序。"""
    def __init__(self):
        self.orders = {}
        self.sequence = 0

    def ranked(self, side):
        sign = -1 if side == "BUY" else 1
        return sorted((o for o in self.orders.values() if o.side == side and o.remaining),
                      key=lambda o: (sign*o.price, o.sequence))

    def add(self, order_id, side, price, quantity, *, continuous=True):
        if order_id in self.orders:
            raise ValueError("重复订单号")
        if side not in {"BUY", "SELL"} or price <= 0 or quantity <= 0:
            raise ValueError("无效内核订单")
        self.sequence += 1
        order = BookOrder(order_id, side, price, quantity, self.sequence)
        self.orders[order_id] = order
        matches = []
        if continuous:
            opposite = "SELL" if side == "BUY" else "BUY"
            for resting in self.ranked(opposite):
                crossed = price >= resting.price if side == "BUY" else price <= resting.price
                if not crossed or not order.remaining:
                    break
                quantity = min(order.remaining, resting.remaining)
                order.remaining -= quantity
                resting.remaining -= quantity
                buy, sell = (order, resting) if side == "BUY" else (resting, order)
                matches.append(Match(buy.order_id, sell.order_id, resting.price, quantity))
        return matches

    def cancel(self, order_id):
        order = self.orders[order_id]
        amount = order.remaining
        order.remaining = 0
        return amount

    def clearing(self):
        buys, sells = self.ranked("BUY"), self.ranked("SELL")
        if not buys or not sells or buys[0].price < sells[0].price:
            return None, 0
        levels = sorted({o.price for o in buys+sells})
        # 区间内累计量相同，取每个区间两端即可保留中间价的完整范围。
        candidates = set(levels)
        for left, right in zip(levels, levels[1:]):
            if right-left > 1:
                candidates.update([left+1, right-1])
        outcomes = []
        for p in candidates:
            buy = sum(o.remaining for o in buys if o.price >= p)
            sell = sum(o.remaining for o in sells if o.price <= p)
            volume = min(buy, sell)
            better_buy = sum(o.remaining for o in buys if o.price > p)
            better_sell = sum(o.remaining for o in sells if o.price < p)
            if volume and better_buy <= volume and better_sell <= volume:
                outcomes.append((p, volume, abs(buy-sell)))
        if not outcomes:
            return None, 0
        maximum = max(x[1] for x in outcomes)
        best = [x for x in outcomes if x[1] == maximum]
        imbalance = min(x[2] for x in best)
        prices = [x[0] for x in best if x[2] == imbalance]
        price = rounded(Decimal(min(prices)+max(prices))/2)
        return price, maximum

    def auction(self):
        price, volume = self.clearing()
        if price is None:
            return []
        buys = [o for o in self.ranked("BUY") if o.price >= price]
        sells = [o for o in self.ranked("SELL") if o.price <= price]
        left, i, j, trades = volume, 0, 0, []
        while left:
            buy, sell = buys[i], sells[j]
            quantity = min(left, buy.remaining, sell.remaining)
            buy.remaining -= quantity
            sell.remaining -= quantity
            left -= quantity
            trades.append(Match(buy.order_id, sell.order_id, price, quantity))
            if not buy.remaining:
                i += 1
            if not sell.remaining:
                j += 1
        return trades

    def levels(self, side, depth=10):
        totals = {}
        for order in self.ranked(side):
            totals[order.price] = totals.get(order.price, 0)+order.remaining
        return sorted(totals.items(), reverse=side == "BUY")[:depth]
