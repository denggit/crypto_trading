#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : batch_analyze.py
@Description: 批量钱包选秀 (V4 Pro 适配) -> 修复指标缺失与黑名单功能
"""
import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm

# 确保能找到 analyze_wallet 模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists("results"):
    os.mkdir("results")

try:
    from analyze_wallet import (
        fetch_history_pagination,
        parse_token_projects,
        get_detailed_scores  # V4 Pro 返回: score, tier, desc, radar
    )
except ImportError:
    print("❌ 错误：在 tools 目录下找不到 analyze_wallet.py")
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
    """ 分析单个钱包，适配 V4 Pro 的 4 参数返回 """
    try:
        # 1. 拉取数据 (根据 API 额度调整样本量)
        txs = await fetch_history_pagination(session, address, max_count=5000)
        if not txs:
            pbar.update(1)
            return None

        # 2. 执行 V4 全量成本法解析
        results = await parse_token_projects(session, txs, address)
        if not results:
            pbar.update(1)
            return None

        # 3. 🔥 核心修复：接收 V4 Pro 的 4 个返回值
        # score=综合评分, tier=评级, desc=状态评价(含置信度), radar=雷达图数据
        score, tier, desc, radar = get_detailed_scores(results)

        # 4. 自动黑名单过滤
        if score < 45 and len(results) >= 3:
            add_to_trash(address)
            pbar.update(1)
            return None
        elif score < 20:
            add_to_trash(address)
            pbar.update(1)
            return None

        # 5. 提取最佳定位 (雷达图中分最高的角色)
        best_role = "未知"
        if radar:
            best_role = max(radar, key=radar.get)

        # 6. 计算基础指标
        import statistics
        wins = [r for r in results if r['is_win']]
        win_rate = len(wins) / len(results)
        total_profit = sum(r['profit'] for r in results)
        max_roi = max([r['roi'] for r in results]) if results else 0
        median_hold = statistics.median([r['hold_time'] for r in results]) if results else 0

        # 提取置信度标识 (根据代币数判断)
        confidence = "高" if len(results) > 10 else "低"

        pbar.update(1)
        return {
            "钱包地址": address,
            "综合评分": score,
            "战力评级": tier,
            "置信度": confidence,  # 🔥 新增指标
            "最佳定位": best_role,  # 🔥 新增指标
            "盈亏比": desc.split("|")[0].split(":")[-1].strip(),
            "总盈亏(SOL)": round(total_profit, 2),
            "胜率": f"{win_rate:.1%}",
            "最大单笔ROI": f"{max_roi:.0%}",
            "中位持仓(分)": round(median_hold, 1),
            "代币数": len(results),
            "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    except Exception as e:
        # 如果报错，可以在此处调试: print(f"Error: {e}")
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
    skip_count = len(all_addresses) - len(addresses)

    if not addresses:
        print(f"🚫 库中所有地址都在黑名单内，或没有新地址。")
        return

    print(f"🚀 启动批量分析 V4 Pro | 任务数: {len(addresses)} (跳过黑名单: {skip_count})")
    pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")

    # 并发限制
    semaphore = asyncio.Semaphore(2)

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
        output_file = f"results/wallet_ranking_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        df.to_excel(output_file, index=False, engine='openpyxl')
        print(f"\n✅ 导出成功: {output_file}")
    else:
        print("\n🏁 分析结果为空，请检查报错或地址列表。")


if __name__ == "__main__":
    asyncio.run(main())
