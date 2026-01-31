#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : tools/batch_analyze_to_excel.py
@Description: 批量钱包选秀 -> 导出 Excel + 自动黑名单 + 动态进度条
"""
import asyncio
import os
import sys
import pandas as pd
import aiohttp
from datetime import datetime
from tqdm.asyncio import tqdm  # 🔥 引入异步进度条库

# 🌟 引入核心分析逻辑
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from analyze_wallet import (
        fetch_history_pagination,
        parse_trades,
        calculate_score_for_mode,
        get_tier_rating
    )
except ImportError:
    print("❌ 错误：找不到 analyze_wallet.py")
    sys.exit(1)

# === ⚙️ 文件配置 ===
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
    """ 分析单个钱包，并更新进度条 """
    try:
        # 1. 拉取数据 (批量模式查 2000 条)
        txs = await fetch_history_pagination(session, address, max_count=2000)
        if not txs:
            pbar.update(1)
            return None

        # 2. 解析
        trades = parse_trades(txs, address)
        if not trades:
            pbar.update(1)
            return None

        # 3. 计算指标
        count = len(trades)
        wins = [t for t in trades if t['roi'] > 0]
        win_rate = len(wins) / count
        total_profit = sum(t['profit'] for t in trades)
        max_roi = max([t['roi'] for t in trades]) if trades else 0
        min_roi = min([t['roi'] for t in trades]) if trades else 0

        import statistics
        hold_times = [t['hold_time'] for t in trades]
        median_hold = statistics.median(hold_times) if hold_times else 0
        sniper_rate = len([t for t in trades if t['hold_time'] < 2]) / count
        recent_win_rate = len([t for t in trades[-10:] if t['roi'] > 0]) / 10

        # 4. 跑分
        scores = {
            "稳健": calculate_score_for_mode('conservative', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                             min_roi, recent_win_rate),
            "激进": calculate_score_for_mode('aggressive', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                             min_roi, recent_win_rate),
            "钻石": calculate_score_for_mode('diamond', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                             min_roi, recent_win_rate)
        }
        best_role, best_score = max(scores.items(), key=lambda item: item[1])
        tier, tier_desc = get_tier_rating(best_score)

        # 自动黑名单
        if best_score < 40:
            add_to_trash(address)
            pbar.update(1)
            return None

        pbar.update(1)  # 🔥 任务完成，进度条加1
        return {
            "钱包地址": address, "综合评分": best_score, "评级": tier, "最佳定位": best_role,
            "总盈亏(SOL)": round(total_profit, 2), "胜率": f"{win_rate:.1%}",
            "最大单笔ROI": f"{max_roi:.0%}", "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    except Exception:
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
        print(f"🚫 已跳过 {skip_count} 个黑名单，无新地址需要分析。")
        return

    print(f"🚀 启动批量选秀 | 总任务: {len(addresses)} | 已跳过黑名单: {skip_count}")

    # 🔥 初始化进度条
    pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")

    # 限制并发，防止 429
    semaphore = asyncio.Semaphore(1)

    async def sem_task(session, addr):
        async with semaphore:
            return await analyze_one_wallet(session, addr, pbar)

    results = []
    async with aiohttp.ClientSession() as session:
        tasks = [sem_task(session, addr) for addr in addresses]
        raw_results = await asyncio.gather(*tasks)
        results = [r for r in raw_results if r is not None]

    pbar.close()  # 记得关闭进度条

    if results:
        df = pd.DataFrame(results).sort_values(by="综合评分", ascending=False)
        output_file = f"wallet_ranking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        df.to_excel(output_file, index=False)
        print(f"\n✅ 导出成功: {output_file}")
    else:
        print("\n🏁 分析完毕，未发现符合标准的地址。")


if __name__ == "__main__":
    asyncio.run(main())