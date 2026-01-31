#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : tools/batch_analyze_to_excel.py
@Description: 批量钱包选秀 -> 导出 Excel 报表
@Usage      :
    1. 确保已安装: pip install pandas openpyxl
    2. 运行: python tools/batch_analyze_to_excel.py
"""
import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pandas as pd

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
    print("❌ 错误：找不到 analyze_wallet.py，请确保它在 tools/ 目录下")
    sys.exit(1)


async def analyze_one_wallet(session, address, idx, total):
    """ 分析单个钱包 (返回字典数据) """
    print(f"[{idx}/{total}] 🔍 正在审计: {address[:6]}...")

    # 1. 拉取数据 (批量模式每人查1000条即可，兼顾速度)
    txs = await fetch_history_pagination(session, address, max_count=1000)
    if not txs: return None

    # 2. 解析
    trades = parse_trades(txs, address)
    if not trades: return None

    # 3. 计算指标
    count = len(trades)
    if count == 0: return None

    wins = [t for t in trades if t['roi'] > 0]
    win_rate = len(wins) / count
    total_profit = sum(t['profit'] for t in trades)
    max_roi = max([t['roi'] for t in trades]) if trades else 0
    min_roi = min([t['roi'] for t in trades]) if trades else 0

    import statistics
    hold_times = [t['hold_time'] for t in trades]
    median_hold = statistics.median(hold_times) if hold_times else 0

    sniper_txs = [t for t in trades if t['hold_time'] < 2]
    sniper_rate = len(sniper_txs) / count

    recent_trades = trades[-10:]
    recent_wins = [t for t in recent_trades if t['roi'] > 0]
    recent_win_rate = len(recent_wins) / len(recent_trades) if recent_trades else 0

    # 4. 跑分 (取最高分身份)
    scores = {
        "稳健": calculate_score_for_mode('conservative', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                         min_roi, recent_win_rate),
        "土狗": calculate_score_for_mode('aggressive', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                         min_roi, recent_win_rate),
        "钻石": calculate_score_for_mode('diamond', win_rate, median_hold, sniper_rate, total_profit, max_roi, min_roi,
                                         recent_win_rate)
    }
    best_role, best_score = max(scores.items(), key=lambda item: item[1])
    tier, tier_desc = get_tier_rating(best_score)

    # 返回结构化数据
    return {
        "钱包地址": address,
        "综合评分": best_score,
        "评级": tier,
        "最佳定位": best_role,
        "评价标签": tier_desc,
        "总盈亏(SOL)": round(total_profit, 2),
        "胜率": f"{win_rate:.1%}",
        "近10单胜率": f"{recent_win_rate:.1%}",
        "最大单笔ROI": f"{max_roi:.0%}",
        "最大回撤": f"{min_roi:.1%}",
        "中位持仓(分)": round(median_hold, 1),
        "秒男率": f"{sniper_rate:.1%}",
        "交易笔数": count,
        "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M")
    }


async def main():
    # 1. 读取地址
    wallet_file = "wallets.txt"
    if not os.path.exists(wallet_file):
        print(f"❌ 找不到 {wallet_file}，请先创建并放入地址！")
        return

    with open(wallet_file, 'r') as f:
        addresses = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not addresses:
        print("⚠️ 地址列表为空")
        return

    print(f"🚀 开始批量分析 {len(addresses)} 个地址，正在导出 Excel...")

    # 2. 并发执行
    semaphore = asyncio.Semaphore(5)  # 稍微快一点，5并发

    async def sem_task(session, addr, idx):
        async with semaphore:
            return await analyze_one_wallet(session, addr, idx, len(addresses))

    results = []
    async with aiohttp.ClientSession() as session:
        tasks = [sem_task(session, addr, i + 1) for i, addr in enumerate(addresses)]
        raw_results = await asyncio.gather(*tasks)
        results = [r for r in raw_results if r is not None]

    # 3. 生成 Excel
    if not results:
        print("❌ 没有获取到有效数据")
        return

    df = pd.DataFrame(results)

    # 按分数倒序排列
    df = df.sort_values(by="综合评分", ascending=False)

    # 文件名加时间戳
    output_file = f"wallet_ranking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    try:
        df.to_excel(output_file, index=False, engine='openpyxl')
        print("\n" + "=" * 50)
        print(f"✅ 成功！结果已保存至: {output_file}")
        print(f"📊 共分析: {len(results)} 个钱包")
        print(f"🏆 S级大神: {len(df[df['综合评分'] >= 90])} 个")
        print("=" * 50)
    except Exception as e:
        print(f"❌ 保存 Excel 失败 (请检查是否已安装 openpyxl): {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
