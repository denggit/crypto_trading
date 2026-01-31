#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 (Pro 版 - 支持突破100分与风控惩罚)
"""
import asyncio
import os
import sys
import argparse
from collections import defaultdict
import statistics
import aiohttp

# 导入配置中的 API Key
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import HELIUS_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000
MIN_SOL_THRESHOLD = 0.1


# =================

async def fetch_history_pagination(session, address, max_count=1000):
    """ 自动翻页拉取交易记录 """
    all_txs = []
    last_signature = None

    print(f"🔍 正在深度审计: {address[:6]}... (自动画像中)")
    print(f"🎯 目标样本: {max_count} 条 (挖掘数据...)")

    while len(all_txs) < max_count:
        batch_limit = 100
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        params = {"api-key": HELIUS_API_KEY, "type": "SWAP", "limit": str(batch_limit)}
        if last_signature: params["before"] = last_signature

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    print(f"❌ API 错误: {resp.status}")
                    break
                data = await resp.json()
                if not data: break

                all_txs.extend(data)
                last_signature = data[-1].get('signature')

                if len(data) < batch_limit: break
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"❌ 网络异常: {e}")
            break

    return all_txs[:max_count]


def parse_trades(transactions, target_wallet):
    """ 解析交易流 """
    positions = defaultdict(list)
    closed_trades = []
    IGNORE_MINTS = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]

    for tx in reversed(transactions):
        if 'tokenTransfers' not in tx: continue
        timestamp = tx.get('timestamp', 0)
        sol_change, token_change, token_mint = 0, 0, ""

        for nt in tx.get('nativeTransfers', []):
            if nt['fromUserAccount'] == target_wallet: sol_change -= nt['amount'] / 1e9
            if nt['toUserAccount'] == target_wallet: sol_change += nt['amount'] / 1e9

        for tt in tx.get('tokenTransfers', []):
            if tt['mint'] in IGNORE_MINTS: continue
            token_mint = tt['mint']
            amt = tt['tokenAmount']
            if tt['fromUserAccount'] == target_wallet: token_change -= amt
            if tt['toUserAccount'] == target_wallet: token_change += amt

        if not token_mint or token_change == 0: continue
        if abs(sol_change) < 0.01 and sol_change != 0: continue

        if token_change > 0 and sol_change < 0:  # BUY
            positions[token_mint].append({"time": timestamp, "cost_sol": abs(sol_change)})

        elif token_change < 0 and sol_change > 0:  # SELL
            if token_mint in positions and positions[token_mint]:
                open_pos = positions[token_mint].pop(0)
                if open_pos['cost_sol'] < MIN_SOL_THRESHOLD: continue

                hold_time = (timestamp - open_pos['time']) / 60
                profit = sol_change - open_pos['cost_sol']
                roi = profit / open_pos['cost_sol'] if open_pos['cost_sol'] > 0 else 0

                closed_trades.append({
                    "token": token_mint,
                    "hold_time": hold_time,
                    "roi": roi,
                    "profit": profit,
                    "cost": open_pos['cost_sol']
                })

    return closed_trades


def calculate_score_for_mode(mode, win_rate, median_hold, sniper_rate, profit, max_roi, max_loss, recent_win_rate):
    """
    🧠 动态多模式评分算法 (Pro版)
    引入：max_loss (最大单笔亏损), recent_win_rate (近期状态)
    """
    score = 100

    # === 模式 A: 稳健中军 (Conservative) ===
    if mode == 'conservative':
        # 1. 胜率 (权重最高)
        if win_rate < 0.5:
            score -= 30
        elif win_rate < 0.6:
            score -= 10
        elif win_rate > 0.75:
            score += 10  # 🔥 加分项：胜率超高

        # 2. 风险控制 (核心升级)
        if max_loss < -0.8:
            score -= 40  # 单笔腰斩80%，直接不合格
        elif max_loss < -0.5:
            score -= 20  # 单笔腰斩50%，扣分

        # 3. 持仓时间
        if median_hold < 10: score -= 30

        # 4. 盈利能力
        if profit < 0: score -= 50

        # 5. 操作频率
        if sniper_rate > 0.2: score -= 20

        # 6. 近期状态 (防止跟到走下坡路的大哥)
        if recent_win_rate < 0.4: score -= 15

    # === 模式 B: 激进先锋 (Aggressive) ===
    elif mode == 'aggressive':
        if max_roi < 5.0:
            score -= 40
        elif max_roi > 20.0:
            score += 10  # 🔥 加分项：抓到过20倍金狗

        if win_rate < 0.3: score -= 20
        if profit < 0 and max_roi < 10.0: score -= 30

        if sniper_rate > 0.5: score -= 5

    # === 模式 C: 钻石手 (Diamond) ===
    elif mode == 'diamond':
        if median_hold < 60:
            score -= 50
        elif median_hold < 1440:
            score -= 10
        elif median_hold > 2880:
            score += 10  # 🔥 加分项：拿单超过2天

        if max_roi < 3.0: score -= 20
        if sniper_rate > 0.1: score -= 30

    return score  # 现在可以超过100分


def get_tier_rating(score):
    """ 获取评级标签 """
    if score >= 110: return "SSS", "🦄 传说级 (可遇不可求)"
    if score >= 100: return "S", "👑 顶级大师 (完美数据)"
    if score >= 85: return "A", "🔥 优秀高手 (值得重仓)"
    if score >= 70: return "B", "👌 良好 (可以跟单)"
    if score >= 60: return "C", "😐 及格 (观察仓位)"
    return "F", "💩 垃圾/韭菜 (千万别跟)"


async def main():
    parser = argparse.ArgumentParser(description="Auto Identity Analyzer Pro")
    parser.add_argument("wallet", help="Target Wallet Address")
    args = parser.parse_args()
    target = args.wallet

    async with aiohttp.ClientSession() as session:
        txs = await fetch_history_pagination(session, target, TARGET_TX_COUNT)
        if not txs: return
        trades = parse_trades(txs, target)
        if not trades: print("⚠️ 无有效交易数据"); return

        # === 1. 基础数据计算 ===
        count = len(trades)
        wins = [t for t in trades if t['roi'] > 0]
        total_profit = sum(t['profit'] for t in trades)

        hold_times = [t['hold_time'] for t in trades]
        median_hold = statistics.median(hold_times) if hold_times else 0

        sniper_txs = [t for t in trades if t['hold_time'] < 2]
        sniper_rate = len(sniper_txs) / count

        win_rate = len(wins) / count
        max_roi = max([t['roi'] for t in trades]) if trades else 0
        min_roi = min([t['roi'] for t in trades]) if trades else 0  # 🔥 最大回撤

        # 计算最近 10 笔的胜率 (Recent Form)
        recent_trades = trades[-10:]
        recent_wins = [t for t in recent_trades if t['roi'] > 0]
        recent_win_rate = len(recent_wins) / len(recent_trades) if recent_trades else 0

        # === 2. 三维雷达扫描 (传入更多参数) ===
        scores = {
            "🛡️ 稳健中军": calculate_score_for_mode('conservative', win_rate, median_hold, sniper_rate, total_profit,
                                                    max_roi, min_roi, recent_win_rate),
            "⚔️ 土狗猎手": calculate_score_for_mode('aggressive', win_rate, median_hold, sniper_rate, total_profit,
                                                    max_roi, min_roi, recent_win_rate),
            "💎 钻石之手": calculate_score_for_mode('diamond', win_rate, median_hold, sniper_rate, total_profit, max_roi,
                                                   min_roi, recent_win_rate)
        }

        # 找出最高分
        best_role, best_score = max(scores.items(), key=lambda item: item[1])
        tier, tier_desc = get_tier_rating(best_score)

        # === 3. 输出可视化报告 ===
        print("\n" + "═" * 60)
        print(f"🧬 钱包战力分析报告 (Pro): {target[:6]}...{target[-4:]}")
        print("═" * 60)

        print(f"📊 核心数据:")
        print(f"   • 总盈亏: {'+' if total_profit > 0 else ''}{total_profit:.2f} SOL")
        print(f"   • 胜  率: {win_rate:.1%} (近10单: {recent_win_rate:.1%})")
        print(f"   • 极值: 🚀{max_roi * 100:.0f}% / 📉{min_roi * 100:.1f}% (最大回撤)")
        print(f"   • 持  仓: {median_hold:.1f} 分钟 (中位数)")

        print("-" * 30)
        print(f"🎯 身份匹配 (雷达):")
        for role, sc in scores.items():
            # 动态进度条，支持超过100分
            bar_len = min(int(sc / 10), 12)
            bar = "█" * bar_len + "░" * (12 - bar_len)
            print(f"   {role}: {bar} {sc}分")

        print("-" * 30)
        print(f"🏆 综合评级: [{tier}级] {best_score} 分")
        print(f"📝 评价标签: {tier_desc}")
        print(f"💡 最佳定位: {best_role}")

        # 智能点评
        if best_score >= 100:
            print("✨ 点评: 无论从胜率还是风控看，都是无可挑剔的六边形战士！")
        elif min_roi < -0.8 and "稳健" in best_role:
            print("⚠️ 警告: 虽然分数高，但有单笔亏损超过80%的记录，请小心炸雷。")

        print("═" * 60)

        if count > 0:
            print("\n📝 最近 5 笔实战:")
            for t in trades[-5:]:
                icon = "🟢" if t['roi'] > 0 else "🔴"
                print(f" {icon} 持仓 {t['hold_time']:>5.1f}m | 投入 {t['cost']:>5.2f} | ROI {t['roi'] * 100:>+6.1f}%")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass