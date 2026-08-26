#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sukebei_scheduler — sukebei.nyaa.si 定时增量爬虫库

逻辑（每轮定时执行）：
  1. 读本地状态文件，取「本地最大已尝试 ID」（首次用 --initial-id）
  2. 抓 sukebei.nyaa.si 列表页 / RSS，取「网站最新 ID」
  3. 比较：
       - 最新 > 本地max → 抓取区间 [本地max+1, 最新] 的详情页，逐条写入 output
       - 最新 <= 本地max → 无新内容，跳过本轮，等待下次定时执行
  4. 404 也算「已尝试」（删除/下架的条目），水位线照常推进，避免每轮重复请求

既可作库被 import，也可命令行直接跑：

    # 单次执行
    python sukebei_scheduler.py --once --initial-id 4609903 --output sukebei.jsonl

    # 每 3600 秒定时执行
    python sukebei_scheduler.py --initial-id 4609903 --output sukebei.jsonl --interval 3600

依赖: curl_cffi, beautifulsoup4
"""

import argparse
import asyncio
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_URL = "https://sukebei.nyaa.si"
VIEW_URL = BASE_URL + "/view/{}"
RSS_URL = BASE_URL + "/?page=rss"

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
TIMEOUT = 25
WATERMARK_SAVE_EVERY = 200          # 每抓 N 条保存一次水位线（崩溃安全）
BACKOFF_429_SEC = 30                # 429 后的冷却

# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_view(html, vid):
    """解析单个 /view/{id} 详情页。无效/404 返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    if soup.find("div", class_="alert-danger"):
        return None
    title_tag = soup.find("h3", class_="panel-title")
    if not title_tag:
        return None
    res = {"id": vid, "title": title_tag.get_text(strip=True)}

    magnet_tag = soup.find("a", href=re.compile(r"^magnet:\?"))
    res["magnet"] = magnet_tag["href"] if magnet_tag else None
    if res["magnet"]:
        m = re.search(r"btih:([a-fA-F0-9]{40})", res["magnet"])
        res["info_hash"] = m.group(1).lower() if m else None
    else:
        res["info_hash"] = None

    res["uploaded_at"] = None
    ts_tag = soup.find(attrs={"data-timestamp": True})
    if ts_tag:
        try:
            ts = int(ts_tag["data-timestamp"])
            res["uploaded_at"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            pass
    if not res["uploaded_at"]:
        for g in soup.find_all(title=re.compile(r"\d{4}-\d{2}-\d{2}")):
            res["uploaded_at"] = g["title"].strip()
            break

    def get_int(eid):
        tag = soup.find(id=eid)
        if tag:
            try:
                return int(tag.get_text(strip=True))
            except Exception:
                return None
        return None
    res["seeders"] = get_int("seeders") or 0
    res["leechers"] = get_int("leechers") or 0

    for row in soup.select(".panel-body .row"):
        cols = row.find_all("div", recursive=False) or row.find_all("div")
        if len(cols) >= 2:
            key = cols[0].get_text(strip=True).rstrip(":")
            if key:
                res[key] = cols[1].get_text(strip=True)
    res["size"] = res.get("File size") or res.get("Size") or ""
    return res


# ---------------------------------------------------------------------------
# 最新 ID
# ---------------------------------------------------------------------------
async def fetch_latest_id(session, retries=3):
    """抓取网站最新 ID。先试 RSS（首条即最新），失败回退列表页。带重试，429/5xx 时退避。"""
    for attempt in range(1, retries + 1):
        try:
            # 1) RSS: 第一条 <link>/<guid> 指向最新详情页
            resp = await session.get(RSS_URL, timeout=30)
            if resp.status_code == 429:
                raise Exception("429")
            resp.raise_for_status()
            m = re.search(r"/view/(\d+)", resp.text)
            if m:
                return int(m.group(1))
            raise Exception("RSS 无 /view/ 链接")
        except Exception as e:
            if attempt < retries:
                print(f"  [!] 最新ID获取失败({e}), {BACKOFF_429_SEC}s 后重试 ({attempt}/{retries})", flush=True)
                await asyncio.sleep(BACKOFF_429_SEC)
            else:
                print(f"  [!] RSS 获取失败({e}), 回退列表页", flush=True)
    # 2) 列表页: 第一个 /view/{id} 即最新
    for attempt in range(1, retries + 1):
        try:
            resp = await session.get(BASE_URL + "/", timeout=30)
            if resp.status_code == 429:
                raise Exception("429")
            resp.raise_for_status()
            m = re.search(r"/view/(\d+)", resp.text)
            if not m:
                raise ValueError("无法从列表页解析最新 ID")
            return int(m.group(1))
        except Exception as e:
            if attempt < retries:
                print(f"  [!] 列表页获取失败({e}), {BACKOFF_429_SEC}s 后重试 ({attempt}/{retries})", flush=True)
                await asyncio.sleep(BACKOFF_429_SEC)
            else:
                raise
    raise ValueError("无法获取网站最新 ID")


# ---------------------------------------------------------------------------
# 本地状态
# ---------------------------------------------------------------------------
def load_state(state_file):
    """读水位线: {max_attempted, last_checked}。文件不存在返回 None。"""
    try:
        if Path(state_file).exists():
            return json.loads(Path(state_file).read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def save_state(state_file, max_attempted):
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    Path(state_file).write_text(
        json.dumps({
            "max_attempted": max_attempted,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def local_max(state_file, initial_id):
    """取本轮起点: 状态水位线存在则用之, 否则 initial_id - 1。"""
    st = load_state(state_file)
    if st and st.get("max_attempted"):
        return int(st["max_attempted"])
    return initial_id - 1


# ---------------------------------------------------------------------------
# 单轮抓取: [start_id, end_id]
# ---------------------------------------------------------------------------
async def crawl_range(start_id, end_id, output, min_delay, max_delay, workers, proxy_url,
                      state_file, progress_cb=None):
    """抓取 [start_id, end_id] 区间详情页, 追加写入 output jsonl。

    返回 (已尝试数, 成功条数)。水位线随抓取推进, 每 WATERMARK_SAVE_EVERY 条落盘一次。
    """
    ids = list(range(start_id, end_id + 1))
    total = len(ids)
    print(f"  [crawl] 区间 {start_id} ~ {end_id} ({total} 条) | 线程 {workers} "
          f"| 延迟 {min_delay}-{max_delay}s | 输出 {output}")

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    attempted = start_id - 1
    found = 0
    lock = asyncio.Lock()

    async with AsyncSession(headers=H, proxies=proxies, impersonate="chrome124") as session:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        fh = open(output, "a", encoding="utf-8")
        try:
            done = [0]  # 已完成计数（含失败）

            async def worker():
                nonlocal attempted, found
                while True:
                    try:
                        vid = await q.get()
                    except asyncio.CancelledError:
                        return
                    try:
                        # 内层 try/except 捕获所有请求错误 —— worker 永不崩溃
                        item = None
                        msg = ""
                        try:
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                            resp = await session.get(VIEW_URL.format(vid), timeout=TIMEOUT)
                            if resp.status_code == 404:
                                msg = "404(已删除)"
                            elif resp.status_code == 429:
                                msg = "429(冷却)"
                                await asyncio.sleep(BACKOFF_429_SEC)
                            else:
                                resp.raise_for_status()
                                item = parse_view(resp.text, vid)
                                msg = "ok" if item else "parse_fail"
                        except Exception as e:
                            msg = f"err:{str(e)[:60]}"
                        if item:
                            async with lock:
                                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                                fh.flush()
                            found += 1
                        # 水位线推进（404/失败也算尝试过）
                        async with lock:
                            if vid > attempted:
                                attempted = vid
                            if (vid - (start_id - 1)) % WATERMARK_SAVE_EVERY == 0:
                                save_state(state_file, attempted)
                        done[0] += 1
                        # 每 50 条打印一次进度，防 Actions 管道缓冲
                        if done[0] % 50 == 0 or done[0] == total:
                            print(f"    [{done[0]}/{total}] #{vid} {msg}", flush=True)
                        if progress_cb:
                            progress_cb(vid, total, found)
                    except Exception as e:
                        print(f"    [warn] #{vid} 未捕获异常: {str(e)[:80]}", flush=True)
                    finally:
                        q.task_done()  # 恰好一次，保证 q.join() 能完成

            q = asyncio.Queue(maxsize=workers * 20)
            tasks = [asyncio.create_task(worker()) for _ in range(workers)]
            for vid in ids:
                await q.put(vid)
            await q.join()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            save_state(state_file, attempted)
        finally:
            fh.close()
    return attempted, found


# ---------------------------------------------------------------------------
# 单轮决策: 比较本地 max 与网站最新 ID
# ---------------------------------------------------------------------------
async def check_and_crawl(initial_id, output, state_file, min_delay, max_delay,
                          workers, proxy_url, progress_cb=None):
    """执行一轮。返回 True 表示本轮抓取了新内容, False 表示无新内容。"""
    start_id = local_max(state_file, initial_id) + 1

    print(f"[*] 本地水位线 max_attempted = {start_id - 1}")
    async with AsyncSession(headers=H, impersonate="chrome110") as s:
        site_max = await fetch_latest_id(s)
    print(f"[*] 网站最新 ID = {site_max}")

    if site_max < start_id:
        print(f"[*] 无新内容 (最新 {site_max} < 本地起点 {start_id}), 停止本轮, 等下次定时执行")
        save_state(state_file, site_max)
        return False

    await crawl_range(start_id, site_max, output, min_delay, max_delay, workers,
                      proxy_url, state_file, progress_cb)
    print(f"[*] 本轮完成, 水位线推进至 {site_max}")
    return True


# ---------------------------------------------------------------------------
# 定时循环
# ---------------------------------------------------------------------------
def run_schedule(initial_id, output, state_file, interval, min_delay, max_delay,
                 workers, proxy_url, once=False, progress_cb=None):
    """定时执行主循环: 每 interval 秒跑一轮 check_and_crawl。"""
    while True:
        started = time.time()
        try:
            asyncio.run(check_and_crawl(initial_id, output, state_file, min_delay,
                                        max_delay, workers, proxy_url, progress_cb))
        except KeyboardInterrupt:
            print("\n[!] 手动中断")
            break
        except Exception as e:
            print(f"[!] 本轮异常: {e}")
        if once:
            break
        elapsed = time.time() - started
        wait = max(1, interval - elapsed)
        print(f"[*] 等待 {wait:.0f}s 后执行下一轮...")
        time.sleep(wait)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="sukebei.nyaa.si 定时增量爬虫")
    p.add_argument("--initial-id", type=int, required=True,
                   help="初始 ID（本地无状态时从它开始）")
    p.add_argument("--output", default="sukebei.jsonl", help="输出 jsonl 文件")
    p.add_argument("--state", default="sukebei_state.json", help="水位线状态文件")
    p.add_argument("--interval", type=int, default=3600, help="定时间隔（秒），默认 3600")
    p.add_argument("--once", action="store_true", help="只执行一轮就退出")
    p.add_argument("--min-delay", type=float, default=0.8)
    p.add_argument("--max-delay", type=float, default=1.3)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--proxy", default=None, help="http(s) 代理，如 socks5://127.0.0.1:10808")
    a = p.parse_args(argv)

    print(f"[*] sukebei 定时爬虫 | initial={a.initial_id} | interval={a.interval}s "
          f"| once={a.once} | output={a.output}")
    run_schedule(a.initial_id, Path(a.output), Path(a.state), a.interval,
                 a.min_delay, a.max_delay, a.workers, a.proxy, once=a.once)


if __name__ == "__main__":
    main(sys.argv[1:])
