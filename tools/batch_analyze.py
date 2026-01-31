#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : batch_analyze.py
@Description: 批量钱包选秀 (适配 V4 全量成本版) -> 修复数据读取 Bug
"""
import asyncio
import os
import sys
import pandas as pd
import aiohttp
from datetime import datetime
from tqdm.asyncio import tqdm

# 🌟 引入核心分析逻辑
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from analyze_wallet import (
        fetch_history_pagination,
        parse_token_projects,  # V4 的解析函数
        get_detailed_scores  # V4 的评分函数 (返回元组)
    )
except ImportError:
    print("❌ 错误：找不到 analyze_wallet.py")
    sys.exit(1)

TRASH_FILE = "wallets_trash.txt"
WALLETS_FILE = "wallets.txt"


def load_trash_list():
    if not os.path.exists(TRASH_FILE): return set()
    with open(TRASH_FILE, 'r') as f:
        return {line.strip() for line in f if line.strip()}


def add_to_trash(address):
    with open(TRASH_FILE, 'a') as f:
        f.write(f"{address}\n")


async def analyze_one_wallet(session, address, pbar):
    """ 分析单个钱包，适配 V4 算法 """
    try:
        # 1. 拉取数据
        txs = await fetch_history_pagination(session, address, max_count=3000)
        if not txs:
            pbar.update(1)
            return None

        # 2. 🔥 使用 V4 解析算法 (全量成本法)
        results = await parse_token_projects(session, txs, address)
        if not results:
            pbar.update(1)
            return None

        # 3. 🔥 修复点：适配 V4 的元组返回格式 (score, tier, desc)
        # 原代码 analysis['total'] 会报错
        score, tier, desc = get_detailed_scores(results)

        # 自动黑名单：低于 45 分自动拉黑
        if score < 45:
            add_to_trash(address)
            pbar.update(1)
            return None

        # 4. 统计基础数据
        wins = [r for r in results if r['is_win']]
        win_rate = len(wins) / len(results)
        total_profit = sum(r['profit'] for r in results)
        max_roi = max([r['roi'] for r in results]) if results else 0

        # V4 的 results 里已经计算好了每个代币的利润，这里直接取平均/中位持仓
        import statistics
        hold_times = [r['hold_time'] for r in results]
        median_hold = statistics.median(hold_times) if hold_times else 0

        pbar.update(1)
        return {
            "钱包地址": address,
            "综合评分": score,
            "评级": tier,
            "状态描述": desc,
            "总盈亏(SOL)": round(total_profit, 2),
            "胜率": f"{win_rate:.1%}",
            "最大单笔ROI": f"{max_roi:.0%}",
            "中位持仓(分)": round(median_hold, 1),
            "代币数": len(results),
            "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    except Exception as e:
        # 如果还是报错，打印出具体的错误信息，方便我们定位
        # print(f"DEBUG Error for {address}: {e}")
        pbar.update(1)
        return None


async def main():
    trash_set = load_trash_list()
    if not os.path.exists(WALLETS_FILE):
        print(f"❌ 找不到 {WALLETS_FILE}")
        return

    with open(WALLETS_FILE, 'r') as f:
        all_addresses = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    addresses = [a for a in all_addresses if a not in trash_set]

    if not addresses:
        print(f"🚫 无新地址需要分析（已跳过黑名单）。")
        return

    print(f"🚀 启动批量分析 V4 版 | 任务数: {len(addresses)}")
    pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")

    semaphore = asyncio.Semaphore(3)

    async def sem_task(session, addr):
        async with semaphore:
            return await analyze_one_wallet(session, addr, pbar)

    async with aiohttp.ClientSession() as session:
        tasks = [sem_task(session, addr) for addr in addresses]
        raw_results = await asyncio.gather(*tasks)
        results = [r for r in raw_results if r is not None]

    pbar.close()

    if results:
        df = pd.DataFrame(results).sort_values(by="综合评分", ascending=False)
        output_file = f"wallet_ranking_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        df.to_excel(output_file, index=False)
        print(f"\n✅ 导出成功: {output_file}")
    else:
        print("\n🏁 分析完成，本次扫描未发现有效数据。请检查 API Key 是否有效。")


if __name__ == "__main__":
    asyncio.run(main())