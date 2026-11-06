# 研究闭环验证记录

验证日期：2026-10-30。环境：Python 3.12.3、NumPy 2.5.4、pandas 3.0.6、PyArrow 26.0.0。
复用第四讲虚拟环境，没有安装或升级依赖，没有启动GPU训练、行情登录或交易接口。

## 回归测试

| 范围 | 命令运行位置 | 结果 |
| --- | --- | --- |
| 新增研究闭环 | 根目录：`lesson04/.venv/bin/python -m unittest discover -s tests/research -v` | 26项通过 |
| 原第四讲 | `lesson04/`：`.venv/bin/python -m unittest discover -s tests -q` | 84项通过 |
| 原第五讲 | `lesson05/`：`../lesson04/.venv/bin/python -m unittest discover -s tests -q` | 21项通过 |

合计131项通过。第四讲测试包含Streamlit bare mode的 `missing ScriptRunContext` 提示，
未造成失败。另通过新增模块AST语法检查与 `git diff --check -- .`。

新增测试覆盖：明确日历窗口、预算限制、表达式内容去重、禁止任意代码与负向窗口、
反馈改变搜索方向、独立收益公式核对、未来泄漏负对照、历史未知状态、
开发期拒绝留出数据与不导出test指标、失败候选参与批内FDR、因子评分与实际买入成分一致、
次日开盘成交与实际佣金/滑点、父子血缘、多轮预算、因子生命周期、行为相关性去重、
拒绝覆盖、封存后拒绝追加、重复审核拒绝、数据/源码/环境/产物变化拒绝、
全失败时不读取留出数据、修改留出行情不影响开发反馈，以及开发异常记录与关闭。

## 合成端到端实验

```bash
lesson04/.venv/bin/python -m quant_research demo --output research_runs/demo_verified
```

- 900交易日×40证券，固定随机种子的持久漂移合成环境。
- 三轮共12次尝试，包含基于开发反馈生成的后继候选。
- 开发结束冻结3个候选；一次历史审核3/3通过，均登记为 `synthetic_control_passed`。
- 最终因子目录：3个合成正对照通过、7个仅开发通过、2个拒绝。
- 批次状态 `reviewed`；再次审核以退出码2拒绝，未重新读取审核数据。

报告：[合成实验](../research_runs/demo_verified/REPORT.md)。
它仅证明管线及正对照行为，不是市场有效性证据。

## 真实行情接口验证

在运行前固定 `configs/research/smoke.json`，使用课程Feather行情与现有BaoStock历史状态。
从原5451列中按 `np.linspace(0, len(columns)-1, 120, dtype=int)` 固定取120只证券，
完整证券顺序保存于manifest。未依据实验表现修改股票池或阈值。

- 初始候选为 `configs/research/seeds.json` 的20日收益与20日市场偏离波动。
- 开发截至2024-12-31，两个向前窗口、两轮共4次尝试。
- 四个候选均未通过完整准入，主要涉及扣费收益、可执行超额、回撤或跨期稳定性。
- 零个冻结审核候选，批次状态 `sealed`，未计算2025年后的审核期指标。
- 对该批次调用 `review` 以退出码2拒绝；没有为了通过而降低收益或稳定性门槛。

报告：[真实行情接口验证](../research_runs/real_smoke_verified/REPORT.md)。
该实验只验证有限证券池的接口与拒绝路径，不是完整全市场因子搜索。

## 产物核验

- 两个最终实验manifest中的源码指纹均与当前 `quant_research`、第四讲回测及第五讲评价源码一致。
- 两个SQLite台账的 `PRAGMA integrity_check` 均为 `ok`。
- 第五讲原有 `examples/course_default/manifest.json` 中的全部评价源码哈希仍与当前源码一致。
- 原课程模块没有修改；没有覆盖已有研究产物。`research_runs/`为忽略目录，
  原始行情、逐日账户和SQLite等运行产物不会自动加入Git。

后续仍需LLM在线提案、跨批次暴露管理、风险中性化和组合增量检验、前瞻观察及失效退役。
详见[研究闭环说明](RESEARCH_LOOP.md)。

## 可视化界面追加验证

2026-10-30新增 `research_app.py` 与 `research_ui/`，复用已安装的Streamlit 1.66.0与Altair。
通过9项界面测试，覆盖批次/候选切换、状态过滤、成交预览、开发期无审核选项、
血缘连线、初始资金高水位回撤、空目录与路径越界，以及浏览前后全部实验文件内容一致。

另以实际 `real_smoke_verified` 和 `demo_verified` 结果运行AppTest：真实行情概览为
4次尝试/2轮/0开发通过/0历史审核通过；合成实验冻结候选能够查看历史审核窗口及成交。
交互后两个批次的所有文件内容和计算源码指纹均保持一致，没有重跑研究或审核。

服务仅监听服务器本机 `127.0.0.1:8503`；首页与 `/_stcore/health` 均返回HTTP 200。
通过VS Code Remote SSH端口转发访问。自动审批拒绝向Tailscale私网发布研究数据，
因此未启动私网或公网监听。
