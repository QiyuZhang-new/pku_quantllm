# 本地模拟交易系统

在 `lesson03` 目录运行。Python 3.10+，数据读取依赖 PyArrow；本次验证使用 Python 3.12 / PyArrow 26.0.0。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m sse_sim demo
```

默认回放 `sse50_20260923` 的三只证券至 09:35，演示下单、成交、撤单、无效申报和 T+1 拒单。输出目录打印在终端，打开其中的 `report.html` 可离线播放十档盘口、历史订单簿和账户变化。

```bash
# 三只证券回放到收盘，所有未成交活动订单自动失效、释放冻结资源
.venv/bin/python -m sse_sim demo --end 15:00:00

# 执行自己的委托计划，使用显式名称引用申报
.venv/bin/python -m sse_sim demo --symbols 601600 \
  --plan samples/order_plan.json --end 09:31:00

# 仅观察行情，不运行演示策略
.venv/bin/python -m sse_sim demo --strategy none --all-symbols
```

还支持 `--cash`（元）、`--initial-shares`（每只证券昨日库存）、`--latency-ms`、`--no-star-permission`、`--data-dir` 和 `--output`。输出目录必须不存在，以免覆盖已有回放结果。数据文件名固定为 `ord_20260923.parquet`、`exe_20260923.parquet`、`snp_20260923.parquet`，目前只提供该交易日的回放入口。

## 委托计划

计划是 JSON 数组。申报包含 `time`、`symbol`、`side`（BUY/SELL）、`price`（元）和 `quantity`（股），可选 `id` 为唯一名称。撤单包含 `action: "cancel"`，通过 `ref` 引用计划中更早提交的名称，或通过 `order_id` 引用系统订单号。示例见 [order_plan.json](../samples/order_plan.json)。

计划按时间稳定排序，同一时间按数组顺序执行，先于同毫秒行情，因此不会使用该毫秒尚未观察的报价。申报行情必须不超过 3 秒；资金、持仓、申报单位等交易校验失败会记录拒单并继续。格式、名称引用、未选择的证券等错误会在创建输出目录前直接拒绝整个计划。申报失败的名称映射为 null，后续撤单记录失败，不会误撤其他订单。

最后一条行情后、截止时间以内的计划仍会执行，但不会制造额外流动性。截止时间之后的计划保留为待执行项，统计见 `summary.json` 的 `plan`。提前结束时保留冻结资源；时钟到达 15:00 即按收盘边界失效，无需该证券恰好有收盘快照。

## 模块和产物

| 模块 | 职责 |
| --- | --- |
| `rules.py` | 整数分、毫秒时间、证券状态和限价申报校验 |
| `matching.py` / `exchange.py` | 合成订单的价格时间优先、开收盘集合竞价及日内生命周期 |
| `broker.py` | 资金与持仓冻结、延迟、部分成交、费用、撤单、收盘失效、T+1 交收 |
| `replay.py` | 分批读取 Parquet、通道业务序列排序、历史剩余量缓存 |
| `plan.py` | 计划预校验、订单名称映射和按时间执行 |
| `report.py` | 离线交互 HTML 报告 |

每次回放保存 `summary.json`、`orders.json`、`trades.json`、`events.jsonl`、`frames.json`、输入 SHA256 清单 `inputs.json` 及 `report.html`。金额字段均为整数分，盘口数量为股。账户守恒和订单数量守恒在运行时检查。

券商成交模型为延迟之后下一份可见快照的十档流动性。自身订单共享每份快照的深度、遵守价格时间优先，同一证券同一时间快照不会重复提供深度；不同时间快照可能包含重复挂单，模型未推断真实队列和市场冲击。集合竞价仅由独立 `Exchange` 演示，不用于历史账户成交。市价、特殊上市状态、盘后固定价格交易仍属于未实现范围。

费用使用 `Fees` 中的教学假设，最低佣金按一笔订单累计计提。T+1 交收通过 `PaperBroker.settlement` 调用，要求提供外部核验的交易日历；下一交易日前需更新证券前收盘价等静态参数。此入口不连接券商实盘。
