# 验证记录

验证日期：2026-10-23。环境：Python 3.12.3、NumPy 2.5.4、pandas 3.0.6、PyArrow 26.0.0，使用 `../lesson04/.venv/bin/python`。

## 行为测试

在 lesson05 目录执行：

```bash
../lesson04/.venv/bin/python -m unittest discover -s tests -v
```

结果：21项测试全部通过。覆盖标签首尾时点与复权、缺失入场价不填充、因子和股票池的因果性、切分边界清洗、测试方向翻转不改变训练方向、缺失收益不重选成分、全量往返成本、并列排序与常数因子、HAC数值及缺失日期间隔、FDR、矩阵对齐与文件校验。

固定种子的900日×160证券合成环境中，强信号通过全部准入规则，噪声未被判为好因子，常数因子判为证据不足。合成数据仅验证评价器行为，不代表市场上的可获利因子。

## 课程行情运行

```bash
../lesson04/.venv/bin/python -m factor_eval --output examples/course_default
```

完成 2020-01-02 至 2026-10-16、1643个交易日、5451证券的课程行情评价。训练边界2023-05-24、验证边界2025-02-06，跨区间与尾部共清洗18个信号日。实际股票池按协议进行当日筛选，最多1000只。

5dr 判为不合格，momentum20 与 volatility20 判为有潜力、测试未完全通过。全部候选均未通过当前完整准入，不调整阈值使其变为通过。原始数据和评价源码的 SHA-256 保存于 [manifest.json](examples/course_default/manifest.json)。

## 独立抽查

```bash
../lesson04/.venv/bin/python tools/audit_example.py
```

抽查15个日期，每个区间各5个日期，直接从原始行情独立展开5dr公式、当日股票池与分组，不调用评价引擎的指标函数。定向RankIC、五个分组收益、基准及往返成本与报告一致；全部评价源码哈希与manifest一致。结果见 [audit.json](examples/course_default/audit.json)。

## 命令行集成

在 `/tmp` 中生成180日×30证券的独立合成行情，成功通过 `--factors none --factor custom_alpha=...parquet --eligible ...csv --config ...json` 运行，得到自定义因子报告及全部CSV/JSON/图表。再次向同一非空目录运行时，以退出码2拒绝覆盖研究记录。
