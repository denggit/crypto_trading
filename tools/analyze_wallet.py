#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/31/26 12:41 PM
@File       : analyze_wallet.py
@Description: 
"""
# !/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 大哥筛选器 - 自动分析钱包的胜率、持仓时间和风格
"""
import asyncio
import aiohttp
import sys
import os
import time
from datetime import datetime
from collections import defaultdict

# 导入配置中的 API Key
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config.settings import API_KEY


# 目标分析地址 (这里填你想面试的大哥地址)
# CANDIDATE_WALLET = "这里填你要查的钱包地址"
# 也可以通过命令行传入 python analyze_wallet.py <address>

async def fetch_history(session, address, limit=100):
    """ 从 Helius 拉取最近交易记录 """
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    params = {
        "api-key": API_KEY,
        "type": "SWAP",
        "limit": str(limit)
    }
    print(f"🔍 正在审计钱包: {address[:6]}... (拉取最近 {limit} 条交易)")

    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                print(f"❌ API 请求失败: {resp.status} - {await resp.text()}")
                return []
            return await resp.json()
    except Exception as e:
        print(f"❌ 网络错误: {e}")
        return []


def parse_trades(transactions, target_wallet):
    """ 解析交易流，还原买卖行为 """
    positions = defaultdict(list)  # 记录买入 {token_mint: [ {price, time, amount}, ... ]}
    closed_trades = []  # 记录已平仓的交易

    # 忽略的代币 (USDC, SOL 等)
    IGNORE_MINTS = [
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    ]

    # Helius 返回的是倒序 (最新的在前)，我们要按时间正序处理
    for tx in reversed(transactions):
        if 'tokenTransfers' not in tx: continue

        timestamp = tx.get('timestamp', 0)
        signature = tx.get('signature', '')

        # 简单解析 Swap
        # 逻辑：支出 SOL = 买入; 获得 SOL = 卖出
        sol_change = 0
        token_change = 0
        token_mint = ""

        native_transfers = tx.get('nativeTransfers', [])
        token_transfers = tx.get('tokenTransfers', [])

        # 计算 SOL 变动
        for nt in native_transfers:
            if nt['fromUserAccount'] == target_wallet: sol_change -= nt['amount'] / 1e9
            if nt['toUserAccount'] == target_wallet: sol_change += nt['amount'] / 1e9

        # 寻找非 SOL 代币变动
        for tt in token_transfers:
            if tt['mint'] in IGNORE_MINTS: continue
            token_mint = tt['mint']
            if tt['fromUserAccount'] == target_wallet: token_change -= tt['tokenAmount']
            if tt['toUserAccount'] == target_wallet: token_change += tt['tokenAmount']

        if not token_mint or token_change == 0: continue

        # 判定买卖
        if token_change > 0 and sol_change < 0:
            # === 买入 ===
            cost = abs(sol_change)
            price = cost / token_change
            positions[token_mint].append({
                "time": timestamp,
                "amount": token_change,
                "cost_sol": cost,
                "sig": signature
            })

        elif token_change < 0 and sol_change > 0:
            # === 卖出 ===
            # 简单 FIFO (先进先出) 匹配买入单
            sell_amt = abs(token_change)
            revenue = sol_change

            if token_mint in positions and positions[token_mint]:
                open_pos = positions[token_mint].pop(0)  # 取出最早的一笔买入

                # 计算持仓时间 (分钟)
                hold_time = (timestamp - open_pos['time']) / 60
                # 计算盈亏
                profit_sol = revenue - open_pos['cost_sol']
                roi = profit_sol / open_pos['cost_sol']

                closed_trades.append({
                    "token": token_mint,
                    "hold_time_min": hold_time,
                    "roi": roi,
                    "profit_sol": profit_sol,
                    "type": "WIN" if roi > 0 else "LOSS"
                })

    return closed_trades


async def main():
    if len(sys.argv) < 2:
        print("用法: python analyze_wallet.py <钱包地址>")
        return

    target = sys.argv[1]

    async with aiohttp.ClientSession() as session:
        txs = await fetch_history(session, target)
        if not txs: return

        trades = parse_trades(txs, target)

        if not trades:
            print("⚠️ 未分析出有效 Swap 交易 (可能是纯转账钱包或数据不足)")
            return

        # === 统计分析 ===
        total_trades = len(trades)
        wins = [t for t in trades if t['roi'] > 0]
        losses = [t for t in trades if t['roi'] <= 0]

        win_rate = len(wins) / total_trades
        avg_hold_time = sum(t['hold_time_min'] for t in trades) / total_trades

        total_profit = sum(t['profit_sol'] for t in trades)

        print("\n" + "=" * 40)
        print(f"📊 钱包体检报告: {target[:6]}...")
        print("=" * 40)
        print(f"📅 样本范围: 最近 {total_trades} 笔已平仓交易")
        print(f"🏆 胜率: {win_rate:.1%} ({len(wins)} 胜 / {len(losses)} 负)")
        print(f"⏳ 平均持仓: {avg_hold_time:.1f} 分钟")
        print(f"💰 净盈利: {total_profit:.4f} SOL")

        print("\n⚖️ 风格判定:")
        if avg_hold_time < 5:
            print("🔴 [极高危] PVP 高频机器人 (3秒男) -> ❌ 别跟！")
        elif avg_hold_time < 30:
            print("🟡 [中风险] 短线土狗猎手 -> ⚠️ 滑点设 15% 小额跟")
        else:
            print("🟢 [推荐] 趋势/波段交易者 -> ✅ 滑点设 10% 放心跟")

        print("\n📝 最近 5 笔战绩:")
        for t in trades[-5:]:
            print(f"  • {t['type']} | 持仓 {t['hold_time_min']:.1f}m | ROI: {t['roi'] * 100:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())