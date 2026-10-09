"""预校验并按账户时钟执行用户委托计划。"""
import json
from .rules import cents, parse_time


class OrderPlan:
    def __init__(self, items, symbols):
        if not isinstance(items, list):
            raise ValueError("委托计划必须为 JSON 数组")
        self.items = []
        self.aliases = {}
        self.index = 0
        aliases = set()
        for number, source in enumerate(items, 1):
            try:
                if not isinstance(source, dict):
                    raise ValueError("计划条目必须为对象")
                item = dict(source)
                item["time_ms"] = parse_time(item["time"])
                action = item.setdefault("action", "submit")
                if action == "submit":
                    if isinstance(item["symbol"], bool):
                        raise ValueError("证券代码无效")
                    item["symbol"] = int(item["symbol"])
                    if item["symbol"] not in symbols:
                        raise ValueError("计划证券未包含在回放证券列表")
                    if item["side"] not in {"BUY", "SELL"}:
                        raise ValueError("买卖方向无效")
                    quantity = item["quantity"]
                    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                        raise ValueError("数量必须为正整数")
                    item["price_cents"] = cents(item["price"])
                    alias = item.get("id")
                    if alias is not None:
                        if not isinstance(alias, str) or not alias or alias in aliases:
                            raise ValueError("计划 id 必须为唯一非空字符串")
                        aliases.add(alias)
                elif action == "cancel":
                    if bool(item.get("order_id")) == bool(item.get("ref")):
                        raise ValueError("撤单必须提供 order_id 或 ref 其中一项")
                    key = "ref" if item.get("ref") else "order_id"
                    if not isinstance(item[key], str):
                        raise ValueError("撤单引用必须为字符串")
                else:
                    raise ValueError("action 仅支持 submit/cancel")
                self.items.append(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"计划第 {number} 条无效：{exc}") from exc
        self.items.sort(key=lambda item: item["time_ms"])
        seen = set()
        for item in self.items:
            if item.get("ref") and item["ref"] not in seen:
                raise ValueError(f"撤单 ref 未指向更早的申报：{item['ref']}")
            if item.get("id"):
                seen.add(item["id"])

    @classmethod
    def load(cls, path, symbols):
        return cls(json.loads(path.read_text(encoding="utf-8")), symbols)

    def run_until(self, broker, now):
        while self.index < len(self.items) and self.items[self.index]["time_ms"] <= now:
            item = self.items[self.index]
            when = item["time_ms"]
            if item["action"] == "cancel":
                oid = self.aliases.get(item["ref"]) if item.get("ref") else item["order_id"]
                broker.cancel(oid, when)
            else:
                oid = broker.submit(item["symbol"], item["side"], item["price_cents"],
                                    item["quantity"], when, order_type=item.get("order_type", "LIMIT"))
                if item.get("id"):
                    self.aliases[item["id"]] = oid
            self.index += 1

    def state(self):
        return {"total": len(self.items), "executed": self.index,
                "pending": len(self.items)-self.index, "aliases": dict(self.aliases)}
