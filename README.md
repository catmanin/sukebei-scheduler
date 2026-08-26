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
  --state state/sukebei_state.json

# 定时执行（每天一次）
python sukebei_scheduler.py --initial-id 4693264 --interval 86400 \
  --state state/sukebei_state.json

# 走代理（配合 xray/rotator 的 socks 代理）
python sukebei_scheduler.py --initial-id 4609903 --proxy socks5://127.0.0.1:10808
```

### 输出文件

- 默认输出到 `data/sukebei_{ts}.jsonl`，`{ts}` 为每轮运行时刻（`YYYYmmdd_HHMMSS`），**每轮一个独立文件，不会混写**
- 区间内**每个 ID 都记录一条**（无论成功/404/429/错误/解析失败），带 `status` 标记：
  - 成功：完整字段 + `"status": "ok"`
  - 404/429/解析失败：`{"id": X, "status": "404"}`（无完整数据）
  - 网络错误：`{"id": X, "status": "error", "error": "..."}`
- 自定义输出路径也可用 `{ts}` 占位符：`--output my/sukebei_{ts}.jsonl`

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--initial-id` | 必填 | 初始 ID（仅当本地无状态文件时生效；已有状态则以状态文件为准） |
| `--output` | `data/sukebei_{ts}.jsonl` | 输出 jsonl 文件（支持 `{ts}` 时间戳占位符） |
| `--state` | `sukebei_state.json` | 水位线状态文件 |
| `--interval` | `3600` | 定时间隔（秒） |
| `--once` | off | 只执行一轮就退出 |
| `--reset` | off | 删除状态文件，从 `--initial-id` 重新开始（重置水位线用） |
| `--max-duration` | `0` | 单轮最大运行秒数，到点主动保存水位线并退出（0=不限） |
| `--min-delay` / `--max-delay` | `0.8` / `1.3` | 请求间隔（秒） |
| `--workers` | `2` | 并发数 |
| `--proxy` | 无 | http(s)/socks 代理 |

## GitHub Actions 定时执行

仓库内置 `.github/workflows/scheduled-crawl.yml`，默认每天自动跑一轮（约 750 条/天的新 ID 一次即可覆盖），结果和状态自动提交回仓库（可 `workflow_dispatch` 手动触发一次）。注意：`initial_id` 仅在本地无状态文件时生效，**重置水位线需在仓库里删除 `state/sukebei_state.json`**（或用本地 `--reset`）。

### 超时与续跑机制（不硬杀）

- 单轮设 `--max-duration 3300`（55 分钟）：到点**主动保存水位线、正常退出**，不是靠 GitHub 超时强杀
- 脚本把剩余数写入 `has_more` 输出；工作流读到 `has_more=true` 就 `gh workflow run` **自触发下一轮 runner**，一直续跑直到抓完
- 没有剩余 ID 时 `has_more=false`，本轮结束，等下一个定时
- `timeout-minutes: 95` 仅作为保险丝，防脚本意外挂死（含 429 重试排空余量）
- 自触发的 run 被 concurrency 组排队，等当前 run 完全结束（水位线已 push）才开始，保证续跑拿到最新水位线

## 作为库使用

```python
from sukebei_scheduler import run_schedule, check_and_crawl, crawl_range, parse_view

# 单轮决策：比较本地 max 与网站最新 ID
# 返回 (has_new, remaining)：has_new=是否抓到新内容, remaining=超时未抓的剩余 ID 数
has_new, remaining = await check_and_crawl(
    initial_id=4609903,
    output="data/sukebei_{ts}.jsonl",
    state_file="state/sukebei_state.json",
    min_delay=0.8, max_delay=1.3, workers=2, proxy_url=None,
)
```

## 依赖

- Python 3.9+
- `curl_cffi`（TLS 指纹模拟）
- `beautifulsoup4`
