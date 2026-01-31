#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 V4 (代币全量成本法 + 实时行情修正)
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
    """ 批量获取代币当前价格 (DexScreener) """
    if not token_mints: return {}
    prices = {}
    # 分批请求，防止 URL 过长
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
    """ 获取当前 SOL 价格用于换算 """
    try:
        async with session.get(
                "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112") as resp:
            data = await resp.json()
            return float(data['pairs'][0]['priceUsd'])
    except:
        return 150.0


async def parse_token_projects(session, transactions, target_wallet):
    """
    V4 核心算法：以代币为单位的“全量统计法”
    计算逻辑：(已卖SOL + 剩余价值) / 总投入成本 - 1
    """
    projects = defaultdict(lambda: {
        "buy_sol": 0.0,
        "sell_sol": 0.0,
        "buy_tokens": 0.0,
        "sell_tokens": 0.0,
        "first_time": 0,
        "last_time": 0
    })

    for tx in reversed(transactions):
        timestamp = tx.get('timestamp', 0)
        sol_in_tx = 0
        token_changes = defaultdict(float)

        # 统计 SOL 变动 (原生 + WSOL)
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

        # 将变动归档到代币项目
        for mint, delta in token_changes.items():
            if projects[mint]["first_time"] == 0: projects[mint]["first_time"] = timestamp
            projects[mint]["last_time"] = timestamp

            if delta > 0:  # 买入
                projects[mint]["buy_tokens"] += delta
                projects[mint]["buy_sol"] += abs(sol_in_tx)
            elif delta < 0:  # 卖出
                projects[mint]["sell_tokens"] += abs(delta)
                projects[mint]["sell_sol"] += sol_in_tx

    # 获取实时行情进行最终清算
    active_mints = [m for m, v in projects.items() if (v["buy_tokens"] - v["sell_tokens"]) > 0]
    prices_usd = await get_current_prices(session, active_mints)
    sol_price_usd = await get_sol_price(session)

    final_results = []
    for mint, data in projects.items():
        if data["buy_sol"] < 0.05: continue  # 过滤极小测试单

        remaining_qty = max(0, data["buy_tokens"] - data["sell_tokens"])
        current_price_sol = (prices_usd.get(mint, 0) / sol_price_usd) if sol_price_usd > 0 else 0
        unrealized_value = remaining_qty * current_price_sol

        total_value = data["sell_sol"] + unrealized_value
        net_profit = total_value - data["buy_sol"]
        roi = (total_value / data["buy_sol"]) - 1 if data["buy_sol"] > 0 else 0

        # 判定卖出进度 (是否已经基本清仓)
        exit_pct = data["sell_tokens"] / data["buy_tokens"] if data["buy_tokens"] > 0 else 0

        final_results.append({
            "token": mint,
            "cost": data["buy_sol"],
            "profit": net_profit,
            "roi": roi,
            "is_win": net_profit > 0,
            "hold_time": (data["last_time"] - data["first_time"]) / 60,
            "exit_status": f"{exit_pct:.0%}"
        })

    return final_results


def get_detailed_scores(results):
    """ 增强版评分：看重真实胜率、盈亏比、以及交易多样性 """
    if not results: return 0, "F", "无数据"

    count = len(results)
    wins = [r for r in results if r['is_win']]
    win_rate = len(wins) / count
    total_profit = sum(r['profit'] for r in results)

    # 核心指标：盈亏比
    avg_win = sum(r['profit'] for r in wins) / len(wins) if wins else 0
    losses = [r for r in results if not r['is_win']]
    avg_loss = abs(sum(r['profit'] for r in losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else (avg_win if avg_win > 0 else 0)

    score = 100
    # 1. 胜率调整 (以代币为单位的胜率极难造假)
    if win_rate < 0.4:
        score -= 30
    elif win_rate > 0.6:
        score += 10

    # 2. 笔数惩罚 (样本置信度)
    if count < 5:
        score *= 0.3
    elif count < 10:
        score *= 0.7

    # 3. 盈亏比奖励
    if profit_factor > 3:
        score += 15
    elif profit_factor < 1:
        score -= 20

    # 4. 极端回撤惩罚
    max_loss_roi = min([r['roi'] for r in results])
    if max_loss_roi < -0.8: score -= 20

    score = min(max(0, score), 120)
    tier = "F"
    if score >= 100:
        tier = "S"
    elif score >= 85:
        tier = "A"
    elif score >= 70:
        tier = "B"

    return round(score, 1), tier, f"盈亏比: {profit_factor:.2f} | 代币数: {count}"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wallet")
    args = parser.parse_args()

    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V4: {args.wallet[:6]}...")
        txs = await fetch_history_pagination(session, args.wallet, TARGET_TX_COUNT)
        results = await parse_token_projects(session, txs, args.wallet)

        if not results:
            print("❌ 未发现有效交易项目")
            return

        score, tier, desc = get_detailed_scores(results)

        print("\n" + "═" * 60)
        print(f"🧬 战力报告 (V4 全量成本版): {args.wallet[:6]}...")
        print("═" * 60)
        print(f"📊 核心汇总:")
        print(
            f"   • 项目胜率: {len([r for r in results if r['is_win']]) / len(results):.1%} (基于{len(results)}个代币)")
        print(f"   • 累计利润: {sum(r['profit'] for r in results):+,.2f} SOL")
        print(f"   • 综合得分: {score} [{tier}级]")
        print(f"   • 状态评价: {desc}")

        print("\n📝 重点项目明细 (按利润排序):")
        results.sort(key=lambda x: x['profit'], reverse=True)
        for r in results[:8]:
            icon = "🟢" if r['is_win'] else "🔴"
            print(
                f" {icon} {r['token'][:6]}.. | 利润 {r['profit']:>+7.2f} | ROI {r['roi'] * 100:>+7.1f}% | 退出度 {r['exit_status']}")
        print("═" * 60)


if __name__ == "__main__":
    asyncio.run(main())
