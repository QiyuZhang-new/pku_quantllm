# 第二讲 Wiki

入口：[架构与源码](architecture.md) · [CTP 接入](ctp.md) · [下单方案](order-plan.md) · [因子研究](../factors/REPORT.md) · [运行证据](../evidence)

学习顺序：原始 CTP 字典 → 标准化 Tick/Order/Trade → 事件总线 → OMS 缓存与路由 → CTA 生命周期 → 独立因子实验。

原始材料存放 `raw/`，不修改上游源码；三个固定 commit 见 `evidence/sources.json`。源码通过 `fetch_sources.py` 恢复。`wiki/` 存消化后的概念、源码位置、局限和关联。每次实验记录写入 `evidence/`，原始结果与解释分开保留。

题目引用的 `llm-wiki.md` 在课程仓库返回 404，腾讯下载页不能直接读取，所以本项目采用上述最小可追溯 Wiki 结构，没有声称读到缺失模板。
