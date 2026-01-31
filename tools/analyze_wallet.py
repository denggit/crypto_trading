#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 V4 Pro (修正版: 修复 SOL 重复计算与多代币归因 Bug)
"""
import argparse
import asyncio
import os
import statistics
import sys
from collections import defaultdict

import aiohttp

# 导入配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import HELIUS_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000
WSOL_MINT = "So11111111111111111111111111111111111111112"


async def fetch_history_pagination(session, address, max_count=3000):
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
    try:
        async with session.get(
                "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112") as resp:
            data = await resp.json()
            return float(data['pairs'][0]['priceUsd'])
    except:
        return 150.0


async def parse_token_projects(session, transactions, target_wallet):
    """ V4 核心算法：修正版全量统计法 """
    projects = defaultdict(lambda: {
        "buy_sol": 0.0, "sell_sol": 0.0, "buy_tokens": 0.0, "sell_tokens": 0.0,
        "first_time": 0, "last_time": 0
    })

    for tx in reversed(transactions):
        timestamp = tx.get('timestamp', 0)
        native_sol_change = 0
        wsol_change = 0
        token_changes = defaultdict(float)

        # 1. 统计原生 SOL 变动
        for nt in tx.get('nativeTransfers', []):
            if nt['fromUserAccount'] == target_wallet: native_sol_change -= nt['amount'] / 1e9
            if nt['toUserAccount'] == target_wallet: native_sol_change += nt['amount'] / 1e9

        # 2. 统计 WSOL 和 其他代币变动
        for tt in tx.get('tokenTransfers', []):
            mint = tt['mint']
            amt = tt['tokenAmount']
            if mint == WSOL_MINT:
                if tt['fromUserAccount'] == target_wallet: wsol_change -= amt
                if tt['toUserAccount'] == target_wallet: wsol_change += amt
            else:
                if tt['fromUserAccount'] == target_wallet: token_changes[mint] -= amt
                if tt['toUserAccount'] == target_wallet: token_changes[mint] += amt

        # ⚡ 核心修复 1: 解决 SOL/WSOL 重复计算问题 (去重合并)
        if native_sol_change * wsol_change > 0:  # 同向变动(都是入或都是出)
            sol_in_tx = native_sol_change if abs(native_sol_change) > abs(wsol_change) else wsol_change
        else:
            sol_in_tx = native_sol_change + wsol_change

        # ⚡ 核心修复 2: 精准归因与拆分
        buys = [m for m, d in token_changes.items() if d > 0]
        sells = [m for m, d in token_changes.items() if d < 0]

        if sol_in_tx < 0:  # 支出 SOL -> 归因为买入成本
            avg_cost = abs(sol_in_tx) / len(buys) if buys else 0
            for mint in buys:
                projects[mint]["buy_sol"] += avg_cost
                projects[mint]["buy_tokens"] += token_changes[mint]
        elif sol_in_tx > 0:  # 收入 SOL -> 归因为卖出收益
            avg_proceeds = sol_in_tx / len(sells) if sells else 0
            for mint in sells:
                projects[mint]["sell_sol"] += avg_proceeds
                projects[mint]["sell_tokens"] += abs(token_changes[mint])

        # 记录时间和代币流转（支持无 SOL 交易）
        for mint, delta in token_changes.items():
            if projects[mint]["first_time"] == 0: projects[mint]["first_time"] = timestamp
            projects[mint]["last_time"] = timestamp
            if sol_in_tx == 0:  # 跨代币兑换等场景
                if delta > 0:
                    projects[mint]["buy_tokens"] += delta
                else:
                    projects[mint]["sell_tokens"] += abs(delta)

    # 3. 计算最终收益 (实时价格修正)
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

    base_score = 100
    if win_rate < 0.4:
        base_score -= 30
    elif win_rate > 0.6:
        base_score += 10
    conf_multiplier = 0.3 if count < 5 else (0.7 if count < 10 else 1.0)

    radar = {
        "🛡️ 稳健中军": int(max(0, base_score - (30 if median_hold < 10 else 0)) * conf_multiplier),
        "⚔️ 土狗猎手": int(max(0, base_score + (20 if profit_factor > 3 else 0)) * conf_multiplier),
        "💎 钻石之手": int(max(0, base_score - (40 if median_hold < 60 else 0)) * conf_multiplier)
    }
    final_score = max(radar.values())
    tier = "S" if final_score >= 100 else ("A" if final_score >= 85 else ("B" if final_score >= 70 else "F"))
    return final_score, tier, f"盈亏比: {profit_factor:.2f} | 代币数: {count}", radar


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wallet")
    args = parser.parse_args()
    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V4 Pro: {args.wallet[:6]}...")
        txs = await fetch_history_pagination(session, args.wallet, TARGET_TX_COUNT)
        results = await parse_token_projects(session, txs, args.wallet)
        if not results: return
        score, tier, desc, radar = get_detailed_scores(results)
        print("\n" + "═" * 60)
        print(f"🧬 战力报告 (V4 Pro): {args.wallet[:6]}...")
        print("═" * 60)
        print(
            f"📊 核心汇总:\n   • 项目胜率: {len([r for r in results if r['is_win']]) / len(results):.1%} (基于{len(results)}个代币)\n   • 累计利润: {sum(r['profit'] for r in results):+,.2f} SOL\n   • 持仓中位: {statistics.median([r['hold_time'] for r in results]):.1f} 分钟")
        print("-" * 30 + f"\n🎯 战力雷达 (置信度:{'高' if len(results) > 10 else '低'}):")
        for role, sc in radar.items():
            print(f"   {role}: {'█' * (sc // 10) + '░' * (10 - (sc // 10))} {sc}分")
        print("-" * 30 + f"\n🏆 综合评级: [{tier}级] {score} 分\n📝 状态评价: {desc}\n" + "-" * 30)
        print("\n📝 重点项目明细 (按利润排序):")
        results.sort(key=lambda x: x['profit'], reverse=True)
        for r in results[:8]:
            print(
                f" {'🟢' if r['is_win'] else '🔴'} {r['token'][:6]}.. | 利润 {r['profit']:>+7.2f} | ROI {r['roi'] * 100:>+7.1f}% | 退出度 {r['exit_status']}")


if __name__ == "__main__":
    asyncio.run(main())
