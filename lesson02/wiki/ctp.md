# 底层 CTP 最小行情 Demo

代码：[ctp_demo.py](../ctp_demo.py)。为了只验证底层接口，用 importlib 加载已安装发行包中的原厂 `vnctpmd` 扩展，不导入自动加载 Gateway 的包顶层，也不安装 GUI。

回调链：`createFtdcMdApi` → `registerFront` → `init` → `onFrontConnected` → `reqUserLogin` → `onRspUserLogin` → `subscribeMarketData` → `onRtnDepthMarketData`。所有行情持续以 JSON Lines 打印。主线程通过 Event 等待，默认一直运行；Ctrl+C 或 `--seconds` 才结束，并调用 `exit()` 释放 API。

登录失败不会订阅；断线清除 logged_in，重连登录后重订阅。回报价格为 NaN/Inf/CTP 无效极大值时忽略。只有收到至少一条有效 tick 且没有 API 拒绝才退出 0；零 tick 退出 2，避免把“程序运行完”写成“行情验证成功”。不打印完整登录回报、账号或密码。

合约参数必须显式提供，期货合约会到期，示例不保证仍是主力。建议通过最新合约列表按成交量/持仓量确认后选择 rb/ag/au 等活跃品种。本次连接尝试使用 rb2701、ag2612、au2612，只是当前日期候选，未用真实成交量核实活跃排序。

课程账号仅写入本地 `config.local.json`（权限 0600，被忽略）；公开样例所有配置值为空。前置白名单仅接受课程指定的 SimNow 行情服务器，完全没有交易发单路径。

实际连接结果看 [ctp_live.log](../evidence/ctp_live.log) 与 [verification.json](../evidence/verification.json)，离线回调测试见 [tests.log](../evidence/tests.log)。两种证据不能互相替代。
