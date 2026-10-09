# 第二讲作业：VeighNa 源码学习、CTP 行情与跳空因子

作者：QiyuZhang-new。对应课程 [issue #2](https://github.com/aslan9/pku_quantllm/issues/2)，并参考老师 [2026-10-07 补交标准](https://github.com/aslan9/pku_quantllm/issues/2#issuecomment-6034194088)。使用 Codex 辅助阅读、开发及验证。

## 已完成与验收边界

| 项目 | 产物与结果 |
|---|---|
| 三模块源码学习 | 固定官方 commit、源码恢复脚本、架构/CTP/CTA Wiki |
| 底层行情 Demo | 直接继承原厂 MdApi，连接、登录、订阅、持续 JSON 打印、重订阅与退出 |
| 离线验证 | 6 测试通过，包括官方 EventEngine 实际运行和未来特征扰动测试 |
| 行情连接实测 | 60 秒尝试，0 tick、未验证登录，退出码 2；不视为行情验收成功 |
| 价格触发下单 | 按题目“先方案后确认”提交完整闭环方案；未发单，真实闭环未完成 |
| 一轮 16 跳空因子 | 冻结 12 ETF/21,120 行日线，全部候选、单次样本外评估和成本压力分析 |
| 因子结论 | 选中 gap_vol20；样本外毛年化 −1.49%，扣费年化 −27.21%，未验证有效性 |

入口：[Wiki](wiki/index.md) · [因子报告](factors/REPORT.md) · [环境](evidence/environment.json) · [验证记录](evidence/verification.json)。不把离线测试或静态编译写成真实成交证据。

## 复现

在 Python 3.12 / Linux x86_64 环境，从仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r lesson02/requirements-research.lock.txt
.venv/bin/python -m pip install vnpy_ctp==6.7.11.4 --no-deps
python3 lesson02/fetch_sources.py
.venv/bin/python -m unittest discover -s lesson02 -p test_homework.py -v
```

CTP 扩展从源码构建，需要 C++ 编译环境与安装器自动提供的构建依赖。本 Demo 直接加载底层扩展，不导入 GUI/Gateway。`--no-deps` 是此底层 Demo 的限定运行方式，不适用于完整 VeighNa 应用。

已保存因子实验结果，脚本遇到已有 summary 会停止，防止覆盖冻结研究。新环境复现实验时使用独立目录：

```bash
cp -a lesson02 /tmp/quantllm-lesson02-reproduce
mv /tmp/quantllm-lesson02-reproduce/factors/results /tmp/quantllm-lesson02-reproduce/factors/results-reference
.venv/bin/python /tmp/quantllm-lesson02-reproduce/factors/research.py
```

复现从冻结 CSV gzip 还原数据并检查指纹。运行相同历史计算不构成新一轮筛选。重新下载数据可运行 `download.py`，但供应商历史修订可能改变指纹。

行情实测需在本地填写 `config.local.json`，仅使用 SimNow 账号；配置样例见 `config.sample.json`。不公开配置或 CTP flow 文件。根据当日合约查询选择标的，示例不保证主力合约：

```bash
.venv/bin/python lesson02/ctp_demo.py --config lesson02/config.local.json --symbols rb2701,ag2612,au2612
```

默认持续运行到 Ctrl+C；可用 `--seconds 60` 做限时验证。零 tick 或 API 错误退出非零。老师新增的真实开平仓闭环需要后续确认方案、可用仿真账号和交易时段再验收，本次提交保留这个缺口。
