#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : main.py
@Description: 智能跟单机器人 (修复版：补全买入参数 + 完善日志)
"""
import argparse
import asyncio
import os
import traceback  # 🔥 引入错误堆栈打印

from config.settings import RPC_URL, COPY_AMOUNT_SOL, SLIPPAGE_BUY, MIN_SMART_MONEY_COST, MIN_LIQUIDITY_USD, MAX_FDV, \
    MIN_FDV, MAX_BUY_TIME
from core.portfolio import PortfolioManager
from services.risk_control import check_token_liquidity, check_is_safe_token
from services.solana.monitor import start_monitor, parse_tx, fetch_transaction_details
from services.solana.trader import SolanaTrader
from utils.logger import logger


async def process_tx_task(session, signature, pm: PortfolioManager):
    try:
        tx_detail = await fetch_transaction_details(session, signature)
        # 如果获取失败，直接返回
        if not tx_detail: return

        trade = parse_tx(tx_detail)
        if not trade or not trade['token_address']:
            return

        token = trade['token_address']

        if trade['action'] == "BUY":
            # --- 1. 大哥试盘过滤 ---
            smart_money_cost = trade.get('sol_spent', 0)
            if smart_money_cost < MIN_SMART_MONEY_COST:
                # 调试日志，平时可关
                # logger.warning(f"📉 [过滤] {token} 买入金额过小: {smart_money_cost:.4f} SOL")
                return

            # --- 2. 基础风控 ---
            is_safe, liq, fdv = await check_token_liquidity(session, token)

            if not is_safe:
                logger.warning(f"🚫 [拦截] 低流动性: {token}")
                return

            if liq < MIN_LIQUIDITY_USD:
                logger.warning(f"💧 [拦截] 池子太小: {token} (${liq:,.0f} < ${MIN_LIQUIDITY_USD:,.0f})")
                return

            if fdv < MIN_FDV:
                logger.warning(f"📉 [拦截] 市值太小: {token} (${fdv:,.0f} < ${MIN_FDV:,.0f})")
                return

            if fdv > MAX_FDV:
                logger.warning(f"📈 [拦截] 市值过大: {token} (${fdv:,.0f} > ${MAX_FDV:,.0f})")
                return

            # 🔥 修复：函数重命名为 check_is_safe_token，逻辑更清晰
            is_safe = await check_is_safe_token(session, token)
            if not is_safe:
                logger.warning(f"🚫 [拦截] 貔貅盘/高风险代币: {token}")
                return

            # --- 3. 次数与资金限制 ---
            buy_times = pm.get_buy_counts(token)
            if buy_times >= MAX_BUY_TIME:
                logger.warning(f"🛑 [风控] {token} 已买入 {buy_times} 次，停止加仓")
                return

            my_balance = await pm.trader.get_token_balance(str(pm.trader.payer.pubkey()), pm.trader.SOL_MINT)
            safe_margin = COPY_AMOUNT_SOL * 2  # 预留2倍Gas费

            if my_balance < safe_margin:
                logger.warning(f"💸 [余额不足] 当前: {my_balance:.4f} SOL，暂停买入")
                return

            # --- 4. 执行买入 ---
            # 🔥 修复日志：打印代币地址！
            logger.info(f"🔍 体检通过 [{token}]: 池子 ${liq:,.0f} | 余额 {my_balance:.2f} SOL | 第 {buy_times + 1} 次")

            async with pm.get_token_lock(token):
                # 双重检查
                if pm.get_buy_counts(token) >= MAX_BUY_TIME:
                    return

                amount_in = int(COPY_AMOUNT_SOL * 10 ** 9)

                # 🔥🔥 核心修复：填入真正的参数，而不是 ... 🔥🔥
                success, est_out = await pm.trader.execute_swap(
                    input_mint=pm.trader.SOL_MINT,  # 用 SOL 买
                    output_mint=token,  # 买这个 Token
                    amount_lamports=amount_in,  # 买多少
                    slippage_bps=SLIPPAGE_BUY  # 滑点
                )

                if success:
                    # 🔥 修复：cost_sol 应该是 SOL 数量，不是 lamports
                    pm.add_position(token, est_out, COPY_AMOUNT_SOL)
                    logger.info(f"✅ 跟单成功: {token} | 仓位已记录")
                else:
                    logger.error(f"❌ 跟单失败: {token} (Swap执行返回False)")

        elif trade['action'] == "SELL":
            # 🔥 修复：添加锁保护，防止并发卖出导致的数据不一致
            async with pm.get_token_lock(token):
                await pm.execute_proportional_sell(token, trade['amount'])

    except Exception as e:
        # 🔥 全局异常捕获：如果哪里再报错，这里会打印出来！
        logger.error(f"💥 处理交易发生崩溃: {e}")
        logger.error(traceback.format_exc())


async def main():
    trader = SolanaTrader(RPC_URL)
    pm = PortfolioManager(trader)

    logger.info("🤖 机器人全系统启动...")

    await asyncio.gather(
        pm.monitor_1000x_profit(),
        pm.monitor_sync_positions(),
        pm.schedule_daily_report(),
        start_monitor(process_tx_task, pm)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Solana Copy Trading Bot')
    parser.add_argument('--proxy', action='store_true', help='开启本地 Clash 代理')
    args = parser.parse_args()

    if args.proxy:
        proxy_url = "http://127.0.0.1:7890"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        logger.info(f"🌍 本地模式: 已启用代理 {proxy_url}")
    else:
        logger.info("☁️ 云端模式: 直连无代理")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程序停止")