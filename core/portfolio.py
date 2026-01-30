#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : core/portfolio.py
@Description: 核心资产管理 (持仓、记账、风控、日报)
"""
import asyncio
from datetime import datetime

import aiohttp

# 导入配置和工具
from config.settings import TARGET_WALLET, SLIPPAGE_SELL, TAKE_PROFIT_ROI
from services.notification import send_email_async
from utils.logger import logger


class PortfolioManager:
    def __init__(self, trader):
        self.trader = trader
        self.portfolio = {}
        self.trade_history = []  # 历史交易记录 (用于日报)
        self.is_running = True

    def _record_history(self, action, token, amount, value_sol):
        """ 内部方法：记录交易历史 """
        self.trade_history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "token": token,
            "amount": amount,
            "value_sol": value_sol
        })

    def add_position(self, token_mint, amount_bought, cost_sol):
        if token_mint not in self.portfolio:
            self.portfolio[token_mint] = {'my_balance': 0, 'cost_sol': 0}
        self.portfolio[token_mint]['my_balance'] += amount_bought
        self.portfolio[token_mint]['cost_sol'] += cost_sol

        # 记录历史
        self._record_history("BUY", token_mint, amount_bought, cost_sol)
        logger.info(f"📝 [记账] 新增持仓 {token_mint[:6]}... | 数量: {self.portfolio[token_mint]['my_balance']}")

    async def execute_proportional_sell(self, token_mint, smart_money_sold_amt):
        # 1. 检查持仓
        if token_mint not in self.portfolio or self.portfolio[token_mint]['my_balance'] <= 0:
            logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 但我未持有，跳过。")
            return

        logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 正在计算比例...")

        # 2. 查询大佬剩余持仓
        smart_money_remaining = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
        total_before_sell = smart_money_sold_amt + smart_money_remaining

        sell_ratio = 1.0
        if total_before_sell > 0:
            sell_ratio = smart_money_sold_amt / total_before_sell
            if sell_ratio > 0.99: sell_ratio = 1.0

        my_holdings = self.portfolio[token_mint]['my_balance']
        amount_to_sell = int(my_holdings * sell_ratio)

        if amount_to_sell < 100: return

        # 3. 执行卖出
        logger.info(f"📉 跟随卖出: {amount_to_sell} (占持仓 {sell_ratio:.2%})")
        success, est_sol_out = await self.trader.execute_swap(
            input_mint=token_mint,
            output_mint=self.trader.SOL_MINT,
            amount_lamports=amount_to_sell,
            slippage_bps=SLIPPAGE_SELL
        )

        if success:
            self.portfolio[token_mint]['my_balance'] -= amount_to_sell

            # 记录历史
            self._record_history("SELL", token_mint, amount_to_sell, est_sol_out)

            # 邮件通知
            msg = f"检测到聪明钱卖出，已跟随卖出。\n\n代币: {token_mint}\n数量: {amount_to_sell}\n比例: {sell_ratio:.1%}"
            asyncio.create_task(send_email_async(f"📉 跟随卖出成功: {token_mint[:6]}...", msg))

            if self.portfolio[token_mint]['my_balance'] < 100 and token_mint in self.portfolio:
                del self.portfolio[token_mint]
                logger.info(f"✅ {token_mint[:6]}... 已清仓完毕")

    async def monitor_sync_positions(self):
        """ 防断网兜底：每20秒检查一次链上状态 """
        logger.info("🛡️ 持仓同步防断网线程已启动 (每20秒检查一次)...")
        while self.is_running:
            if not self.portfolio:
                await asyncio.sleep(5)
                continue

            for token_mint in list(self.portfolio.keys()):
                try:
                    my_data = self.portfolio[token_mint]
                    if my_data['my_balance'] <= 0: continue

                    # 查链上余额
                    sm_balance = await self.trader.get_token_balance(TARGET_WALLET, token_mint)

                    # 如果大佬没币了，但我还有，说明漏单了
                    if sm_balance < 1:
                        logger.warning(f"😱 发现异常！持有 {token_mint[:6]}... 但大佬余额为 0！")
                        logger.warning(f"🛡️ 触发防断网机制：立即强制清仓！")
                        await self.force_sell_all(token_mint, my_data['my_balance'], -0.99)
                except Exception as e:
                    logger.error(f"同步检查异常: {e}")
            await asyncio.sleep(20)

    async def monitor_1000x_profit(self):
        """ 止盈监控 """
        logger.info("💰 收益监控线程已启动...")
        # trust_env=True 走代理
        async with aiohttp.ClientSession(trust_env=True) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue
                for token_mint in list(self.portfolio.keys()):
                    try:
                        data = self.portfolio[token_mint]
                        if data['my_balance'] <= 0: continue

                        # 询价
                        quote = await self.trader.get_quote(session, token_mint, self.trader.SOL_MINT,
                                                            data['my_balance'])
                        if quote:
                            curr_val = int(quote['outAmount'])
                            cost = data['cost_sol']
                            roi = (curr_val / cost) - 1 if cost > 0 else 0

                            if roi >= TAKE_PROFIT_ROI:
                                logger.warning(f"🚀 触发 {roi * 100:.0f}% 止盈！{token_mint} 强平！")
                                await self.force_sell_all(token_mint, data['my_balance'], roi)
                    except Exception as e:
                        logger.error(f"盯盘异常: {e}")
                await asyncio.sleep(10)

    async def force_sell_all(self, token_mint, amount, roi):
        """ 强制卖出辅助函数 """
        success, est_sol_out = await self.trader.execute_swap(
            token_mint, self.trader.SOL_MINT, amount, SLIPPAGE_SELL
        )
        if success:
            self._record_history("SELL_FORCE", token_mint, amount, est_sol_out)

            if roi == -0.99:
                subject = f"🛡️ 防断网风控: {token_mint[:6]}..."
                msg = f"检测到聪明钱已清仓，机器人已补救卖出。\n\n代币: {token_mint}"
            else:
                subject = f"🚀 暴富止盈: {token_mint[:6]}..."
                msg = f"触发 1000% 止盈！\n\n代币: {token_mint}\n收益率: {roi * 100:.1f}%\n动作: 全仓卖出"

            asyncio.create_task(send_email_async(subject, msg))
            if token_mint in self.portfolio:
                del self.portfolio[token_mint]

    async def schedule_daily_report(self):
        """ 每日日报调度器 """
        logger.info("📅 日报调度器已启动 (每天 09:00 发送)...")
        while self.is_running:
            now = datetime.now()
            target_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

            if now >= target_time:
                from datetime import timedelta
                target_time += timedelta(days=1)

            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"⏳ 距离发送日报还有 {sleep_seconds / 3600:.1f} 小时")

            await asyncio.sleep(sleep_seconds)
            await self.send_daily_summary()
            await asyncio.sleep(60)

    async def send_daily_summary(self):
        """ 生成并发送日报 """
        logger.info("📊 正在生成每日日报...")
        # trust_env=True 走代理
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                # 1. 获取 SOL 价格 (USDC)
                usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                quote = await self.trader.get_quote(session, self.trader.SOL_MINT, usdc_mint, 1 * 10 ** 9)
                sol_price = float(quote['outAmount']) / 10 ** 6 if quote else 0

                # 2. 查询钱包 SOL 余额
                balance_resp = await self.trader.rpc_client.get_balance(self.trader.payer.pubkey())
                sol_balance = balance_resp.value / 10 ** 9

                # 3. 计算持仓总价值 (SOL)
                holdings_val_sol = 0
                holdings_details = ""

                if self.portfolio:
                    for mint, data in self.portfolio.items():
                        qty = data['my_balance']
                        if qty > 0:
                            q = await self.trader.get_quote(session, mint, self.trader.SOL_MINT, qty)
                            val = int(q['outAmount']) / 10 ** 9 if q else 0
                            holdings_val_sol += val
                            holdings_details += f"- {mint[:6]}...: 持有 {qty}, 价值 {val:.2f} SOL\n"

                total_asset_sol = sol_balance + holdings_val_sol
                total_asset_usd = total_asset_sol * sol_price

                # 4. 统计
                buy_count = sum(1 for x in self.trade_history if x['action'] == 'BUY')
                sell_count = sum(1 for x in self.trade_history if 'SELL' in x['action'])

                report = f"""
【📅 每日交易与资产报告】
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💰 资产概览:
-------------------
• SOL 价格: ${sol_price:.2f}
• 钱包余额: {sol_balance:.4f} SOL
• 持仓价值: {holdings_val_sol:.4f} SOL
• 总计资产: {total_asset_sol:.4f} SOL (≈ ${total_asset_usd:.2f})

📊 交易统计 (累计):
-------------------
• 买入次数: {buy_count}
• 卖出次数: {sell_count}

👜 当前持仓明细:
{holdings_details if holdings_details else "(空仓)"}

🤖 机器人状态: 正常运行中
"""
                await send_email_async("📊 [日报] 资产与交易总结", report)

            except Exception as e:
                logger.error(f"生成日报失败: {e}")
