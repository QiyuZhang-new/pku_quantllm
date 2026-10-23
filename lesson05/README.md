# 第五讲：基于 5dr 的因子评价体系

这套工具回答的是：**一个因子能否稳定地预测未来五日的横截面收益，扣除成本后是否还有选股价值？** 输出明确结论、失败原因和可追溯数据，不用单一 IC 或漂亮的累计收益图决定因子好坏。

这里同时保留两个定义，避免把收益标签和候选因子混淆：

- 候选因子 `5dr(t) = adjusted_close(t) / adjusted_close(t−5) − 1`，即过去五个交易日收益率。
- 评价标签 `forward_5dr(t) = adjusted_open(t+6) / adjusted_open(t+1) − 1`。收盘形成信号，次日开盘进入，五个交易日后的开盘退出。

`adjusted_price = raw_price × cumadj`，收益率计算不前填缺失值。过去五日收益若呈负 IC，训练期会把方向固定为 −1，即评价五日反转的有效性；**负 IC 不直接等于坏因子**。验证与测试阶段都不能重新翻方向。

## 直接运行

从 GitHub 获取作业后，可先运行不依赖课程行情的演示：

```bash
git clone https://github.com/QiyuZhang-new/pku_quantllm.git
cd pku_quantllm/lesson05
uv sync
uv run python -m factor_eval --demo --output results/demo
```

真实行情的下载、字段说明与校验方法见 [第四讲数据说明](../lesson04/REQUIREMENTS.md)；下载后放入 `lesson04/feathers/`，运行 `uv run python -m factor_eval --output results/my_first_run`。

在当前仓库可以复用第四讲已有环境，不需重新下载行情：

```bash
cd /home/zqy/Personal-Growth/database/course/course-homework/QuantLLM/lesson05

# 评价 5dr、20日动量、20日波动率，生成报告
../lesson04/.venv/bin/python -m factor_eval --output results/my_first_run

# 只评价 5dr
../lesson04/.venv/bin/python -m factor_eval --factors 5dr --output results/only_5dr

# 无真实行情也能检查系统：强信号、纯噪声、常数三个对照
../lesson04/.venv/bin/python -m factor_eval --demo --output results/demo

# 验证时间对齐、无未来选股、成本和正负对照等核心行为
../lesson04/.venv/bin/python -m unittest discover -s tests -v

# 从原始行情独立抽查已保存的默认5dr报告，不调用评价引擎的指标函数
../lesson04/.venv/bin/python tools/audit_example.py
```

新环境使用 Python 3.11+，在 `lesson05` 下执行 `uv sync`，之后以 `uv run python` 替换上面的解释器。依赖见 [pyproject.toml](pyproject.toml)。行情默认读取 `../lesson04/feathers/{open,close,cumadj,amt}.feather`，也可通过 `--data /path/to/feathers` 指定；行情不在本目录重复存储。

运行完成后打开输出目录的 `report.html`，它包含图表且无需联网；`REPORT.md` 适合在编辑器中阅读。输出目录非空会拒绝覆盖，新的研究使用新的目录。

本次默认数据的完整结果已保存在 [评价报告](examples/course_default/REPORT.md) 和 [离线图表报告](examples/course_default/report.html)。测试区间为 2025-02-07 起至可观测完整标签的最后信号日，单边成本10bp：

| 因子 | 训练冻结方向 | 测试 RankIC | 测试五日净多头超额 | 结论 |
| --- | --- | --- | --- | --- |
| 5dr | −1（五日反转） | 0.0277 | −0.327% | 不合格 |
| momentum20 | −1（二十日反转） | 0.0741 | −0.123% | 有潜力，测试未完全通过 |
| volatility20 | −1（低波动） | 0.0753 | −0.202% | 有潜力，测试未完全通过 |

5dr 有正向排序信息，但分层不单调、扣费超额为负，因此当前定义未通过准入。结果没有为了得到“好因子”而调整阈值。

## 如何判定好坏

先按交易日期切分：默认前 50% 训练、中间 25% 验证、最后 25% 测试。训练集仅确定因子方向；验证集用于判断是否值得继续；最后测试集审核这个已经固定的定义。每段尾部的跨区间标签被剔除：五日收益从次日开盘开始，因此需要剔除六个信号日。

如需明确的日历边界：

```bash
../lesson04/.venv/bin/python -m factor_eval \
  --train-end 2022-12-30 --valid-end 2024-12-31 \
  --output results/fixed_calendar
```

所有阈值都是本作业的**可配置研究准入标准**，并非市场通用定律。默认在验证期和测试期分别检查以下门槛：

| 维度 | 默认门槛 | 解决的问题 |
| --- | --- | --- |
| 样本与数据 | 有效 IC ≥126 日；训练方向也需 ≥126 日 | 小样本无法判定 |
| 覆盖率 | 因子 ≥80%；标签 ≥98%；有效 IC 日和完整组合日各 ≥80% | 稀疏因子、未来缺失与退化横截面 |
| 预测能力 | 定向后 RankIC ≥0.02；HAC t ≥2；BH-FDR q ≤0.05 | 有方向、有强度，且不只靠重叠样本放大显著性 |
| 经济价值 | 净多头超额 >0；净多空价差 >0 | 能预测但收益不足以覆盖成本 |
| 分层效果 | 分组编号与平均收益的 Spearman 相关 ≥0.6 | 收益是否随评分大体上升 |
| 时间稳定 | 正 IC 月份比例 ≥55%，每月至少10个有效观测 | 是否只靠少数时期 |
| 起点稳定 | 五个非重叠起点中至少60%净多头超额为正，每个起点需 ≥12 期 | 是否只挑中了一个有利调仓日 |

结论有四类：

| 结论 | 含义 |
| --- | --- |
| 好因子（当前研究样本通过） | 数据、预测、收益、稳定性全部通过 |
| 有潜力，但样本外未完全通过 | 验证准入通过，测试方向仍为正，但至少一项审核失败 |
| 不合格（当前定义与样本） | 数据足够，但验证准入失败或测试方向失效 |
| 证据不足 | 样本量、覆盖或完整组合证据不够，不能直接认定好坏 |

另给 100 分的诊断分：预测40、收益30、稳定20、数据10。分数用于定位短板，**只要有硬门槛未过，就不能靠总分变成好因子**。每项实际值、门槛、是否通过都写入 `gates.csv`。

## 收益与统计口径

股票池仅使用当日已知数据：正价格、正复权因子、正成交额，累计有效价格观测至少120日，过去20日平均成交额至少1亿元，再取成交额最高的1000只。`--top-n 0` 可关闭数量上限；默认成交额单位按元解释，接入其他数据时需调整 `--min-adv`。

先按信号日因子分组，再读取未来标签。并列值采用平均秩，不按未来收益或证券顺序拆组，因而组数内的股票数可能不相等。少于五种因子值时不做五组组合评价；常数因子不产生有效 IC。少量离散取值的因子可以减少 `groups` 后单独建立研究协议。

Q1 是定向后的低分组，Q5 是高分组。基准是同一有效因子股票池的等权收益。任何计划成分缺少入场/退出价格，该组当期收益为缺失，基准也要求全部计划成分完整；不事后删除、重新排序或重新分配权重。这会减少收益样本，所以必须同时审核组合完整日期比例；保留下来的日期仍可能存在缺失选择偏差，完整数据是后续验证的前提。

单边成本默认10bp，包含佣金、滑点等的情景假设，不代表某个市场的实际费率。每个五日期间独立完整建仓和平仓：

```text
净多头收益 = Q5 − 2 × 单边成本
净多头超额 = Q5 − 同池等权基准 − 2 × 单边成本
净多空价差 = Q5 − Q1 − 4 × 单边成本
```

这是一种保守的全量往返成本压力分析，没有利用留仓来降低成本。额外输出双倍成本结果与多空盈亏平衡单边成本；压力测试是补充信息，不改变冻结的默认准入规则。`top_name_turnover` 仅为相隔五日高分组的成员替换比例，不是金额换手，也不拿它代替真实成交成本。

每日都计算一个未来五日收益，会产生重叠观测。系统使用 Bartlett 核的 Newey–West/HAC 均值检验，默认10阶滞后，配置至少为 `horizon−1`；缺失日期保留原有交易日间隔。置信区间与双侧 p 值使用大样本正态近似。ICIR 定义为均值/标准差，不机械乘 `sqrt(252)` 年化。

五个固定起点分别按原始交易日行号每五日抽样，不能先删除缺失行再抽样。图中展示各相位**已观测净超额之和**，缺失时断线，不能称为资金净值。多空价差按多空各1元名义头寸计，未计融券费，不能把它当作全额资本组合收益；`long_short_linear_annual` 也仅是均值×252/5的线性尺度。

本次同时检验的全部因子参与 BH-FDR 校正，无效因子按 p=1 保留在检验家族。它不能校正未披露的历史试验，也不能证明相关因子彼此独立。候选结果按输入顺序展示；不要根据测试结果调整阈值，再把相同测试期称为新的样本外。

## 接入自己的因子

支持 CSV、Feather、Parquet 宽表：`date` 列加证券列，或 Parquet 的日期索引。日期为无时区、唯一升序交易日；证券列与行情严格一致，缺失因子值填 NaN，不填0。

```csv
date,000001.SZ,000002.SZ
2020-01-02,0.18,-0.27
2020-01-03,0.20,-0.10
```

上面仅展示格式，正式输入必须包含与行情相同的全部日期和证券列。可以在生成文件前显式 `factor.reindex(index=close.index, columns=close.columns)`。不要填补尚不可知的因子，也不要因未来停牌/退市提前删除证券。

```bash
# 只评自定义因子；可重复提供 --factor，同时进行批量 FDR 校正
../lesson04/.venv/bin/python -m factor_eval --factors none \
  --factor my_alpha=/path/to/my_alpha.parquet \
  --factor another_alpha=/path/to/another_alpha.csv \
  --output results/custom_batch

# 加入同一格式的信号日股票池掩码：1=可选，其他值及未知状态均排除
../lesson04/.venv/bin/python -m factor_eval --factors 5dr \
  --eligible /path/to/historical_eligible.parquet \
  --output results/with_historical_status
```

如果因子是模型输出，模型训练也必须按历史窗口完成；评价器无法从数值矩阵判断其上游是否泄漏未来。`eligible` 必须只用当时已知信息构造，不能把后来知道的退市/风险状态提前过滤。

Python 接口可直接传入日期×证券 DataFrame：

```python
from factor_eval import Config, load_market, build_factors, evaluate_batch

market = load_market("../lesson04/feathers")
factors = build_factors(market)       # 也可以替换为 {"my_alpha": frame}
results = evaluate_batch(market, factors, Config())
for result in results:
    print(result.name, result.summary["verdict"], result.summary["failed_gates"])
```

完整研究协议可用 `--config protocol.json` 冻结；键名与 `Config` 一致，JSON 优先于命令行参数。可参考 [protocol.default.json](protocol.default.json)。改变股票池、方向选择、成本、分组或阈值都属于新研究，不应覆盖已有结果。

## 文件与证据

```text
factor_eval/data.py     行情校验、5dr因子、复权标签、信号日股票池
factor_eval/stats.py    Pearson/RankIC、HAC、BH-FDR
factor_eval/core.py     数据切分、训练方向、分组、判定硬门槛
factor_eval/report.py   离线HTML、Markdown、图表和CSV
factor_eval/cli.py      运行入口、自定义因子、输入和源码哈希
tests/                 时间因果性、统计与组合会计、强信号/噪声对照
examples/course_default/  本次课程行情评价结果
```

每次运行生成 `report.html`、`REPORT.md`、`leaderboard.csv`、`manifest.json`；每个 `factor_XX/` 包含 `summary.json`、`gates.csv`、`daily.csv`、`monthly.csv`、`yearly.csv`、`offsets.csv` 和 `diagnostics.png`。manifest 保存数据与源码 SHA-256、版本、日期边界、参数和候选顺序。

本次 [验证记录](VERIFICATION.md) 包含21项测试、15日期独立抽查以及自定义因子命令行集成检查；抽查的具体日期和结果见 [audit.json](examples/course_default/audit.json)。

默认行情曾在 lesson04 中用于研究，本次 test 是**按时间留出的历史审核区间**，不应声称是从未看过的前瞻数据。当前系统尚未自动接入历史 ST、涨跌停成交、行业、市值及融券可得性，也未做行业/规模中性化。结论表示当前定义下的横截面选股研究证据，不能证明独立 alpha；实盘判断还需要相应暴露控制和可成交回测。

指标定义参考 [Qlib 官方评价文档](https://qlib.readthedocs.io/en/stable/component/report.html)；HAC 方法参考 [statsmodels 官方说明](https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html)。本实现独立使用 pandas/numpy，没有声称运行 Qlib 或 statsmodels。
