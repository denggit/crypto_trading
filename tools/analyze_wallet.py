#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 V4 Pro (全量成本算法 + 视觉增强系统)
"""
import asyncio
import os
import sys
import argparse
from collections import defaultdict
import statistics
import aiohttp
from datetime import datetime

# 导入配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import HELIUS_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000
WSOL_MINT = "So11111111111111111111111111111111111111112"


async def fetch_history_pagination(session, address, max_count=3000):
    """ 带自动重试的翻页抓取 """
    all_txs = []
    last_signature = None
    retry_count = 0
    while len(all_txs) < max_count:
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        params = {"api-key": HELIUS_API_KEY, "type": "SWAP", "limit": 100}
        if last_signature: params["before"] = last_signature
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_count += 1
                    await asyncio.sleep(retry_count * 2)
                    continue
                if resp.status != 200: break
                data = await resp.json()
                if not data: break
                all_txs.extend(data)
                last_signature = data[-1].get('signature')
                if len(data) < 100: break
                await asyncio.sleep(0.1)
        except:
            break
    return all_txs[:max_count]


async def get_current_prices(session, token_mints):
    """ 批量获取实时价格 """
    if not token_mints: return {}
    prices = {}
    mints_list = list(token_mints)
    for i in range(0, len(mints_list), 30):
        chunk = mints_list[i:i + 30]
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get('pairs', [])
                    for p in pairs:
                        if p.get('chainId') == 'solana':
                            prices[p['baseToken']['address']] = float(p.get('priceUsd', 0))
        except:
            continue
    return prices


async def get_sol_price(session):
    """ 获取 SOL 价格 """
    try:
        async with session.get(
                "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112") as resp:
            data = await resp.json()
            return float(data['pairs'][0]['priceUsd'])
    except:
        return 150.0


async def parse_token_projects(session, transactions, target_wallet):
    """ V4 核心算法：以代币为单位的全量统计法 """
    projects = defaultdict(lambda: {
        "buy_sol": 0.0, "sell_sol": 0.0, "buy_tokens": 0.0, "sell_tokens": 0.0,
        "first_time": 0, "last_time": 0
    })

    for tx in reversed(transactions):
        timestamp = tx.get('timestamp', 0)
        sol_in_tx = 0
        token_changes = defaultdict(float)

        for nt in tx.get('nativeTransfers', []):
            if nt['fromUserAccount'] == target_wallet: sol_in_tx -= nt['amount'] / 1e9
            if nt['toUserAccount'] == target_wallet: sol_in_tx += nt['amount'] / 1e9

        for tt in tx.get('tokenTransfers', []):
            mint = tt['mint']
            amt = tt['tokenAmount']
            if mint == WSOL_MINT:
                if tt['fromUserAccount'] == target_wallet: sol_in_tx -= amt
                if tt['toUserAccount'] == target_wallet: sol_in_tx += amt
            else:
                if tt['fromUserAccount'] == target_wallet: token_changes[mint] -= amt
                if tt['toUserAccount'] == target_wallet: token_changes[mint] += amt

        for mint, delta in token_changes.items():
            if projects[mint]["first_time"] == 0: projects[mint]["first_time"] = timestamp
            projects[mint]["last_time"] = timestamp
            if delta > 0:
                projects[mint]["buy_tokens"] += delta
                projects[mint]["buy_sol"] += abs(sol_in_tx)
            elif delta < 0:
                projects[mint]["sell_tokens"] += abs(delta)
                projects[mint]["sell_sol"] += sol_in_tx

    active_mints = [m for m, v in projects.items() if (v["buy_tokens"] - v["sell_tokens"]) > 0]
    prices_usd = await get_current_prices(session, active_mints)
    sol_price_usd = await get_sol_price(session)

    final_results = []
    for mint, data in projects.items():
        if data["buy_sol"] < 0.05: continue
        rem = max(0, data["buy_tokens"] - data["sell_tokens"])
        curr_p = (prices_usd.get(mint, 0) / sol_price_usd) if sol_price_usd > 0 else 0
        unrealized = rem * curr_p
        total_val = data["sell_sol"] + unrealized
        net_profit = total_val - data["buy_sol"]
        roi = (total_val / data["buy_sol"]) - 1 if data["buy_sol"] > 0 else 0
        exit_pct = data["sell_tokens"] / data["buy_tokens"] if data["buy_tokens"] > 0 else 0

        final_results.append({
            "token": mint, "cost": data["buy_sol"], "profit": net_profit, "roi": roi,
            "is_win": net_profit > 0, "hold_time": (data["last_time"] - data["first_time"]) / 60,
            "exit_status": f"{exit_pct:.0%}"
        })
    return final_results


def get_detailed_scores(results):
    """ 综合评分与雷达数据生成 """
    if not results: return 0, "F", "无数据", {}

    count = len(results)
    wins = [r for r in results if r['is_win']]
    win_rate = len(wins) / count
    total_profit = sum(r['profit'] for r in results)
    median_hold = statistics.median([r['hold_time'] for r in results])

    avg_win = sum(r['profit'] for r in wins) / len(wins) if wins else 0
    losses = [r for r in results if not r['is_win']]
    avg_loss = abs(sum(r['profit'] for r in losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else (avg_win if avg_win > 0 else 0)

    # 基础分
    base_score = 100
    if win_rate < 0.4:
        base_score -= 30
    elif win_rate > 0.6:
        base_score += 10

    # 样本置信度惩罚
    conf_multiplier = 1.0
    if count < 5:
        conf_multiplier = 0.3
    elif count < 10:
        conf_multiplier = 0.7

    # 雷达图逻辑
    radar = {
        "🛡️ 稳健中军": int(max(0, base_score - (30 if median_hold < 10 else 0)) * conf_multiplier),
        "⚔️ 土狗猎手": int(max(0, base_score + (20 if profit_factor > 3 else 0)) * conf_multiplier),
        "💎 钻石之手": int(max(0, base_score - (40 if median_hold < 60 else 0)) * conf_multiplier)
    }

    final_score = max(radar.values())
    tier = "F"
    if final_score >= 100:
        tier = "S"
    elif final_score >= 85:
        tier = "A"
    elif final_score >= 70:
        tier = "B"

    return final_score, tier, f"盈亏比: {profit_factor:.2f} | 代币数: {count}", radar


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wallet")
    args = parser.parse_args()

    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V4 Pro: {args.wallet[:6]}...")
        txs = await fetch_history_pagination(session, args.wallet, TARGET_TX_COUNT)
        results = await parse_token_projects(session, txs, args.wallet)

        if not results:
            print("❌ 未发现有效交易项目")
            return

        score, tier, desc, radar = get_detailed_scores(results)

        print("\n" + "═" * 60)
        print(f"🧬 战力报告 (V4 Pro): {args.wallet[:6]}...{args.wallet[-4:]}")
        print("═" * 60)
        print(f"📊 核心汇总:")
        print(
            f"   • 项目胜率: {len([r for r in results if r['is_win']]) / len(results):.1%} (基于{len(results)}个代币)")
        print(f"   • 累计利润: {sum(r['profit'] for r in results):+,.2f} SOL")
        print(f"   • 持仓中位: {statistics.median([r['hold_time'] for r in results]):.1f} 分钟")

        print("-" * 30)
        print(f"🎯 战力雷达 (置信度:{'高' if len(results) > 10 else '低'}):")
        for role, sc in radar.items():
            bar = "█" * (sc // 10) + "░" * (10 - (sc // 10))
            print(f"   {role}: {bar} {sc}分")

        print("-" * 30)
        print(f"🏆 综合评级: [{tier}级] {score} 分")
        print(f"📝 状态评价: {desc}")

        # 战术建议
        if tier in ["S", "A"]:
            best_role = max(radar, key=radar.get)
            print(f"🚀 最佳定位: {best_role}")
            print(f"✅ 建议配置: {'Bot B (稳健)' if '稳健' in best_role else 'Bot A (激进)'}")
        else:
            print("❌ 建议配置: 不推荐跟单 (样本不足或表现不佳)")
        print("═" * 60)

        print("\n📝 重点项目明细 (按利润排序):")
        results.sort(key=lambda x: x['profit'], reverse=True)
        for r in results[:8]:
            icon = "🟢" if r['is_win'] else "🔴"
            print(
                f" {icon} {r['token'][:6]}.. | 利润 {r['profit']:>+7.2f} | ROI {r['roi'] * 100:>+7.1f}% | 退出度 {r['exit_status']}")
        print("═" * 60)


if __name__ == "__main__":
    asyncio.run(main())