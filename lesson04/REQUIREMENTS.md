# 第四讲：回测 —— 实践课要求与资料

> 来源：[pku_quantllm issue #8](https://github.com/aslan9/pku_quantllm/issues/8)（发布于 2026-10-16 08:07 UTC）
> 数据来源：[Release `data-backtest-20261016`](https://github.com/aslan9/pku_quantllm/releases/tag/data-backtest-20261016)

## 一、课堂要求

1. 请选课同学坐到前四排座位
2. 需要现场带电脑，并准备好你的各路 agent
3. 需要的相关数据请见下方链接，可以提前下载好

> 说明：issue #8 正文只包含以上课堂纪律与数据下载指引，**未给出明确的作业题目或交付物清单**。具体交付要求以课堂现场通知为准。

## 二、本讲主题

**第四讲：回测**

## 三、课程数据

- [下载 `feathers.zip`（约 152 MiB）](https://github.com/aslan9/pku_quantllm/releases/download/data-backtest-20261016/feathers.zip)
- [Release 页面与文件说明](https://github.com/aslan9/pku_quantllm/releases/tag/data-backtest-20261016)

压缩包包含 7 个 Feather 日频矩阵：

| 文件 | 含义 | 大小 |
| --- | --- | ---: |
| `open.feather` | 开盘价 | 32,297,546 B |
| `high.feather` | 最高价 | 32,377,890 B |
| `low.feather` | 最低价 | 32,156,250 B |
| `close.feather` | 收盘价 | 32,431,858 B |
| `amt.feather` | 成交额 | 52,398,330 B |
| `num_trades.feather` | 成交笔数 | 37,495,202 B |
| `cumadj.feather` | 累计复权因子 | 5,877,058 B |

- 数据范围：**2020-01-02 至 2026-10-16**
- 每个矩阵：**1,643 个交易日 × 5,451 只证券**
- SHA-256：`5766950f2a105244867b842709cf4bad890e4e11df0eebeeaa4fec42dc05803d`

## 四、下载与校验

```bash
cd lesson04
curl -sSL -o feathers.zip \
  https://github.com/aslan9/pku_quantllm/releases/download/data-backtest-20261016/feathers.zip

sha256sum feathers.zip
# 期望：5766950f2a105244867b842709cf4bad890e4e11df0eebeeaa4fec42dc05803d
```

解包（去掉 macOS 的 `__MACOSX/` 与 `.DS_Store` 噪声）：

```bash
python3 -c "
import zipfile, os, shutil
z = zipfile.ZipFile('feathers.zip')
for i in z.infolist():
    if i.is_dir() or '__MACOSX' in i.filename or i.filename.endswith('.DS_Store'):
        continue
    dest = os.path.realpath(i.filename)
    assert dest.startswith(os.path.realpath('.') + os.sep), i.filename
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with z.open(i) as s, open(dest, 'wb') as d:
        shutil.copyfileobj(s, d)
"
```

## 五、本地落盘与校验结果

本目录已完成下载与解包（2026-10-16）：

```
lesson04/
├── REQUIREMENTS.md      # 本文件
├── feathers.zip         # 159,096,263 B（与 Release 资产等大）
├── .gitignore
└── feathers/
    ├── open.feather
    ├── high.feather
    ├── low.feather
    ├── close.feather
    ├── amt.feather
    ├── num_trades.feather
    └── cumadj.feather
```

校验记录：

| 检查项 | 结果 |
| --- | --- |
| SHA-256 | ✅ 与 issue 公布值一致 |
| 文件大小 | ✅ 159,096,263 B，与 Release 资产一致 |
| 解包内容 | ✅ 仅 7 个 `.feather`，无路径穿越 |
| 数据形状 | ✅ `(1643, 5452)` = `date` 列 + 5,451 只证券 |
| 日期范围 | ✅ `2020-01-02` → `2026-10-16` |

## 六、数据读取示例与注意事项

```python
import pandas as pd

close = pd.read_feather("feathers/close.feather")
# shape (1643, 5452)；首列为 date(datetime64[ns])，其余 5451 列为 float64 价格
# 列名为证券代码，如 000001.SZ、000002.SZ、……、689009.SH

close = close.set_index("date")        # 转成 日期 × 证券 的宽表
```

注意事项（据实际检查）：

- 首列是 `date`，**不是索引**，读入后需 `set_index("date")`。
- 各矩阵含较多缺失值（约 **12.5%** NaN），对应停牌、未上市或已退市区间，回测时需自行处理对齐与填充。
- 价格类矩阵（`open/high/low/close`）为**未复权价**，做收益率计算需配合 `cumadj.feather` 的累计复权因子。
- `feathers/` 与 `feathers.zip` 合计约 370 MB，已在 `.gitignore` 中排除，不入库。

## 七、作业提交方式

据[课程仓库 README](https://github.com/aslan9/pku_quantllm#课程讨论与作业提交)：

- 作业在**对应讲次主 Issue 下评论提交仓库链接**（本讲为 issue #8），不要为一次作业单独新建 Issue。
- 单独提问请新建 Issue，并写明讲次、运行环境、复现步骤、报错和已尝试的处理。
- **禁止提交账号、密码、AuthCode、密钥、完整资金快照或其他凭据。** 本地配置加入 `.gitignore`，公开仓库只保留无值样例。

## 八、Python 环境

使用 Python 3.12 和 `uv` 管理本目录的 `.venv`。依赖声明见 `pyproject.toml`，精确版本由 `uv.lock` 固定。

```bash
source .venv/bin/activate
python --version
uv sync --locked
```

基础依赖为 `numpy`、`pandas`、`pyarrow` 和 `matplotlib`；默认安装的开发依赖包括 `ipykernel` 和 `uv`。在 Notebook 编辑器中选择本目录的 `.venv/bin/python` 作为内核即可。

网页工作台另使用 `streamlit` 和 `altair`，已加入同一环境和锁文件。运行 `streamlit run app.py` 后打开 `http://127.0.0.1:8501`，详细使用方法见 [README.md](README.md#可视化工作台)。

若当前执行环境限制了默认缓存目录的写入，可以使用 `uv --cache-dir /tmp/quantllm-lesson04-uv-cache sync --locked`。

若代理环境中并行下载速度异常，可在命令前添加 `UV_CONCURRENT_DOWNLOADS=2`，降低下载并发。
