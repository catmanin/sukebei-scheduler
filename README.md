# sukebei-scheduler

sukebei.nyaa.si 定时增量爬虫库 —— 给定初始 ID，每轮定时执行时抓取网站最新 ID，与本地最大 ID 比较，有新内容才抓取，没有则停止等待下次执行。

## 工作原理（每轮定时执行）

1. 读本地状态文件 `state/sukebei_state.json`，取「本地最大已尝试 ID」（首次用 `--initial-id`）
2. 抓 sukebei.nyaa.si 列表页 / RSS，取「网站最新 ID」
3. 比较：
   - `最新 > 本地max` → 抓取区间 `[本地max+1, 最新]` 的详情页，逐条写入 `data/sukebei.jsonl`
   - `最新 <= 本地max` → 无新内容，跳过本轮，等待下次定时执行
4. 404 也算「已尝试」（删除/下架的条目），水位线照常推进，避免每轮重复请求

## 本地使用

```bash
pip install -r requirements.txt

# 单次执行（首次：给定初始 ID）
python sukebei_scheduler.py --once --initial-id 4693264 \
  --output data/sukebei.jsonl --state state/sukebei_state.json

# 定时执行（每天一次）
python sukebei_scheduler.py --initial-id 4693264 --interval 86400 \
  --output data/sukebei.jsonl --state state/sukebei_state.json

# 走代理（配合 xray/rotator 的 socks 代理）
python sukebei_scheduler.py --initial-id 4609903 --proxy socks5://127.0.0.1:10808
```

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--initial-id` | 必填 | 初始 ID（本地无状态时从它开始） |
| `--output` | `sukebei.jsonl` | 输出 jsonl 文件 |
| `--state` | `sukebei_state.json` | 水位线状态文件 |
| `--interval` | `3600` | 定时间隔（秒） |
| `--once` | off | 只执行一轮就退出 |
| `--min-delay` / `--max-delay` | `0.8` / `1.3` | 请求间隔（秒） |
| `--workers` | `2` | 并发数 |
| `--proxy` | 无 | http(s)/socks 代理 |

## GitHub Actions 定时执行

仓库内置 `.github/workflows/scheduled-crawl.yml`，默认每天自动跑一轮（约 750 条/天的新 ID 一次即可覆盖），结果和状态自动提交回仓库（可 `workflow_dispatch` 手动触发一次，传入 `initial_id` 覆盖起始值）。

## 作为库使用

```python
from sukebei_scheduler import run_schedule, check_and_crawl, crawl_range, parse_view

# 单轮决策：比较本地 max 与网站最新 ID，返回 True=本轮抓了新内容
has_new = await check_and_crawl(
    initial_id=4609903,
    output="data/sukebei.jsonl",
    state_file="state/sukebei_state.json",
    min_delay=0.8, max_delay=1.3, workers=2, proxy_url=None,
)
```

## 依赖

- Python 3.9+
- `curl_cffi`（TLS 指纹模拟）
- `beautifulsoup4`
