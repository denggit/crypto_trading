#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : main.py
@Description: 智能跟单机器人 (入口)
"""
import asyncio

from config.settings import RPC_URL, COPY_AMOUNT_SOL, SLIPPAGE_BUY, MIN_LIQUIDITY_USD, MIN_FDV, MAX_FDV
from core.portfolio import PortfolioManager
from services.risk_control import check_token_liquidity
from services.solana_monitor import start_monitor, parse_tx, fetch_transaction_details
from services.solana_trader import SolanaTrader
from utils.logger import logger


async def process_tx_task(session, signature, pm: PortfolioManager):
    tx_detail = await fetch_transaction_details(session, signature)
    trade = parse_tx(tx_detail)
    if not trade or not trade['token_address']: return

    token = trade['token_address']

    if trade['action'] == "BUY":
        # 1. 风控
        is_safe, liq, fdv = await check_token_liquidity(session, token)
        if not is_safe:
            logger.warning(f"⚠️ 无法获取数据: {token}")
            return

        logger.info(f"🔍 体检: 池子 ${liq:,.0f} | 市值 ${fdv:,.0f}")
        if liq < MIN_LIQUIDITY_USD or fdv < MIN_FDV or fdv > MAX_FDV:
            return

        # 2. 执行买入
        logger.info(f"🎯 正在跟单买入: {token}")
        amount_in = int(COPY_AMOUNT_SOL * 10 ** 9)
        success, est_out = await pm.trader.execute_swap(
            pm.trader.SOL_MINT, token, amount_in, SLIPPAGE_BUY
        )
        if success:
            pm.add_position(token, est_out, amount_in)

    elif trade['action'] == "SELL":
        await pm.execute_proportional_sell(token, trade['amount'])


async def main():
    # 1. 初始化服务
    trader = SolanaTrader(RPC_URL)

    # 2. 初始化核心逻辑
    pm = PortfolioManager(trader)

    logger.info("🤖 机器人全系统启动...")

    # 3. 运行所有任务
    await asyncio.gather(
        pm.monitor_1000x_profit(),
        pm.monitor_sync_positions(),
        pm.schedule_daily_report(),
        start_monitor(process_tx_task, pm)
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程序停止")
