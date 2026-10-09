# 第三节实践课：上交所教学模拟交易系统

本作业使用 2026-09-23 的课堂行情，完成交易规则校验、撮合演示、账户冻结与 T+1、历史回放、自定义委托计划，以及成交观察和金融交易知识报告。

## 阅读报告

- [图文 Markdown 报告：从六笔成交理解金融交易](reports/trading_knowledge/REPORT.md)：8 张图，讲解盘口、撮合、交易费用、盈亏归因、T+1、成交率、部分成交及回测边界。
- [图文报告便携包](reports/trading_knowledge_report.zip)：下载解压后保留正文与插图目录结构。
- [原全天交互报告下载](https://github.com/QiyuZhang-new/pku_quantllm/releases/download/lesson03-report-20261009/report.html)：下载后用本地浏览器打开。
- [运行参数和委托计划说明](sse_sim/README.md)。

## 安装与验证

从仓库根目录进入 `lesson03`，使用 Python 3.10+：

```bash
cd lesson03
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

本次验证使用 Python 3.12；52 项测试通过。合成 Parquet 测试无需下载完整课堂数据。

## 数据与回放

从[第三节实践课 issue #6](https://github.com/aslan9/pku_quantllm/issues/6) 下载课堂数据 `sse50_20260923.zip`，将以下三个文件放在 `lesson03/sse50_20260923/`：

```text
sse50_20260923/
  ord_20260923.parquet
  exe_20260923.parquet
  snp_20260923.parquet
```

```bash
# 短时演示，输出目录由程序打印
.venv/bin/python -m sse_sim demo

# 全天基线，输出目录必须尚不存在
.venv/bin/python -m sse_sim demo --end 15:00:00 --output runs/runtime_full_day

# 自定义委托及撤单
.venv/bin/python -m sse_sim demo --symbols 601600 \
  --plan samples/order_plan.json --end 09:31:00

# 在全天基线完成后，复现成交观察及 9 组对照实验
.venv/bin/python scripts/observe_trading.py

# 可选：重绘报告插图
.venv/bin/python -m pip install -r requirements-report.txt
.venv/bin/python scripts/build_trading_figures.py
```

完整原始行情、虚拟环境、缓存和临时 `runs/` 产物由 `.gitignore` 排除；报告引用的精选订单、成交、实验统计及 SHA256 存放在 `reports/trading_knowledge/evidence/` 和 `evidence.json`。数据字段和小切片见 [samples](samples/README.md)。

## 实现与验证结果

| 内容 | 实现 |
| --- | --- |
| 价格与申报规则 | 整数分记账、时段、数量、涨跌停、连续竞价有效申报范围 |
| 独立撮合 | 价格/时间优先、部分成交、开收盘集合竞价、当日失效 |
| 模拟账户 | 冻结现金及持仓、累计费用、撤单、T+1、日内单调时钟 |
| 行情回放 | Parquet 分批读取、通道序列排序、历史剩余订单簿 |
| 委托计划 | 格式预校验、订单名称引用、最后一条行情后的计划执行 |
| 可视化 | 离线交互 HTML、中文 PNG/SVG 插图与 Markdown 报告 |

三只证券全天基线处理 717,398 条事件、生成 6 笔模拟成交；费用合计 49.93 元。成交观察发现小单交易费用、初始库存风险、T+1 可卖量和快照时间分辨率都会影响结果解释。

历史账户采用延迟后的下一份可见十档快照近似成交，缺少真实排队和市场冲击；集合竞价由独立内核演示。系统不连接实盘，实验结果不能证明真实可成交或策略有效。
