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
    MIN_FDV, MAX_POSITION_SOL, MAX_BUY_COUNTS_HARD_LIMIT
from core.portfolio import PortfolioManager
from services.notification import send_email_async
from services.risk_control import check_token_liquidity, check_is_safe_token
from services.solana.monitor import start_monitor, parse_tx, fetch_transaction_details
from services.solana.trader import SolanaTrader
from utils.logger import logger


async def process_tx_task(session, signature, pm: PortfolioManager):
    """
    处理交易任务
    
    Args:
        session: aiohttp会话
        signature: 交易签名
        pm: PortfolioManager实例
    """
    try:
        logger.debug(f"🔍 开始处理交易: {signature[:16]}...")
        tx_detail = await fetch_transaction_details(session, signature)
        # 如果获取失败，直接返回
        if not tx_detail:
            logger.warning(f"⚠️ 无法获取交易详情: {signature[:16]}... (可能交易还未被索引)")
            return

        trade = parse_tx(tx_detail)
        if not trade or not trade['token_address']:
            logger.debug(f"⚠️ 交易解析失败或非代币交易: {signature[:16]}... (可能是普通转账或其他操作)")
            return

        token = trade['token_address']
        action = trade.get('action', 'UNKNOWN')
        logger.debug(f"📊 解析到交易: {action} | 代币: {token[:16]}...")

        if trade['action'] == "BUY":
            # --- 1. 大哥试盘过滤 ---
            smart_money_cost = trade.get('sol_spent', 0)
            if smart_money_cost < MIN_SMART_MONEY_COST:
                logger.debug(f"📉 [过滤] {token[:16]}... 买入金额过小: {smart_money_cost:.4f} SOL < {MIN_SMART_MONEY_COST} SOL")
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

            # --- 3. 资金敞口限制 (双重熔断逻辑) ---
            
            # 获取当前已投入成本
            current_cost = pm.get_position_cost(token)
            
            # 【熔断 1】金额风控：防止归零风险
            # 逻辑：(已花掉的钱 + 这次要花的钱) 是否超过 MAX_POSITION_SOL？
            if current_cost + COPY_AMOUNT_SOL > MAX_POSITION_SOL:
                logger.warning(f"🛑 [金额熔断] {token[:16]}... 总投入将超限: {current_cost:.2f} + {COPY_AMOUNT_SOL:.2f} > {MAX_POSITION_SOL:.2f} SOL")
                return

            # 【熔断 2】频次风控：防止高频刷单/技术滥用
            # 逻辑：是否买入次数过于夸张（超过 MAX_BUY_COUNTS_HARD_LIMIT）？
            buy_times = pm.get_buy_counts(token)
            if buy_times >= MAX_BUY_COUNTS_HARD_LIMIT:
                logger.warning(f"🛑 [频次熔断] {token} 买入次数异常 ({buy_times})，强制停止")
                return

            # --- 4. 钱包余额检查 ---
            my_balance = await pm.trader.get_token_balance(str(pm.trader.payer.pubkey()), pm.trader.SOL_MINT)
            safe_margin = COPY_AMOUNT_SOL * 2  # 预留2倍Gas费

            if my_balance < safe_margin:
                logger.warning(f"💸 [余额不足] 当前: {my_balance:.4f} SOL，暂停买入")
                return

            # --- 5. 执行买入 ---
            # 🔥 修复日志：打印代币地址和成本信息！
            logger.info(f"🔍 体检通过 [{token}]: 池子 ${liq:,.0f} | 余额 {my_balance:.2f} SOL | 当前成本 {current_cost:.2f} SOL | 第 {buy_times + 1} 次")

            async with pm.get_token_lock(token):
                # 双重检查（防止并发）
                current_cost_check = pm.get_position_cost(token)
                if current_cost_check + COPY_AMOUNT_SOL > MAX_POSITION_SOL:
                    logger.warning(f"🛑 [双重检查失败] {token} 金额熔断: 当前成本 {current_cost_check:.2f} + 本次 {COPY_AMOUNT_SOL:.2f} > 上限 {MAX_POSITION_SOL:.2f} SOL")
                    return
                
                buy_times_check = pm.get_buy_counts(token)
                if buy_times_check >= MAX_BUY_COUNTS_HARD_LIMIT:
                    logger.warning(f"🛑 [双重检查失败] {token} 频次熔断: 买入次数 {buy_times_check} >= 上限 {MAX_BUY_COUNTS_HARD_LIMIT}")
                    return

                amount_in = int(COPY_AMOUNT_SOL * 10 ** 9)
                logger.info(f"💰 开始执行买入: {token} | 金额: {COPY_AMOUNT_SOL:.4f} SOL ({amount_in} lamports)")

                # 🔥🔥 核心修复：填入真正的参数，而不是 ... 🔥🔥
                success, est_out = await pm.trader.execute_swap(
                    input_mint=pm.trader.SOL_MINT,  # 用 SOL 买
                    output_mint=token,  # 买这个 Token
                    amount_lamports=amount_in,  # 买多少
                    slippage_bps=SLIPPAGE_BUY  # 滑点
                )

                if success:
                    # 🔥 修复：cost_sol 应该是 SOL 数量，不是 lamports
                    # 先记录买入次数，判断是否为第一次买入
                    buy_times_before = pm.get_buy_counts(token)
                    pm.add_position(token, est_out, COPY_AMOUNT_SOL)
                    logger.info(f"✅ 跟单成功: {token} | 预计获得: {est_out} | 仓位已记录")
                    
                    # 📧 只有第一次买入时才发送邮件通知
                    if buy_times_before == 0:
                        msg = f"✅ 首次买入交易成功\n\n代币: {token}\n买入数量: {est_out}\n成本: {COPY_AMOUNT_SOL:.4f} SOL"
                        async def safe_send_email():
                            try:
                                await send_email_async(f"📈 买入通知: {token}", msg)
                            except Exception as e:
                                logger.error(f"⚠️ 邮件发送失败: {e}")
                        asyncio.create_task(safe_send_email())
                else:
                    logger.error(f"❌ 跟单失败: {token} | Swap执行返回False，请查看上方详细错误日志")

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