# 本次策略补充提交

主结果见 [独立审计报告](../ASSIGNMENT_AUDITED_REPORT.md)。源码、主实验逐笔成交/持仓/账户、全部实验聚合、净值图、失败报告与源码快照可直接浏览。

为了控制仓库体积，其余较大的对照实验CSV及旧反转实验Feather明细收录在 [后续策略结果](followup-results.tar.gz)、[第一轮结果](round1-results.tar.gz)和[旧反转结果](reversal-results.tar.gz)，保留原始文件内容和相对路径。需要查看全部逐笔明细或对已有结果重新审计时，请先在 `lesson04/` 执行：

```bash
tar -xzf submission/followup-results.tar.gz
tar -xzf submission/round1-results.tar.gz
tar -xzf submission/reversal-results.tar.gz
```

压缩包只包含 `research/assignment/results/`、`research/assignment/followup_results/`、`research/results/reversal_default/` 三个结果目录，解包会还原相同结果文件；若已自行修改这些目录，请先备份或解压到独立目录。

课程原始行情不上传，请按 REQUIREMENTS.md 获取；历史ST和交易状态缓存通过匿名下载脚本重建，未上传本地环境、凭据或私人仓库其他资料。完整重跑命令见报告。
