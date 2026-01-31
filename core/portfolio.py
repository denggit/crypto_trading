#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : core/portfolio.py
@Description: 核心资产管理 (支持动态止盈比例配置)
"""
import asyncio
import json
import os
import aiohttp
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

# 导入配置和工具
# 🔥 引入新参数 TAKE_PROFIT_SELL_PCT
from config.settings import TARGET_WALLET, SLIPPAGE_SELL, TAKE_PROFIT_ROI, REPORT_HOUR, REPORT_MINUTE, TAKE_PROFIT_SELL_PCT
from services.notification import send_email_async
from utils.logger import logger

# 数据文件路径
DATA_DIR = "data"
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")


class PortfolioManager:
    def __init__(self, trader):
        self.trader = trader
        self.portfolio = {}  # 当前持仓
        self.trade_history = []  # 历史记录
        self.is_running = True
        
        # 锁与缓存
        self.locks = defaultdict(asyncio.Lock)
        self.buy_counts_cache = {}   
        self.sell_counts_cache = {}  

        # 线程池
        self.calc_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="StatsCalc")

        # 初始化加载
        self._ensure_data_dir()
        self._load_data()
        self._rebuild_counts_cache()

    def _ensure_data_dir(self):
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

    def _load_data(self):
        if os.path.exists(PORTFOLIO_FILE):
            try:
                with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
                    self.portfolio = json.load(f)
                logger.info(f"📂 已恢复持仓记忆: {len(self.portfolio)} 个代币")
            except Exception as e:
                logger.error(f"❌ 读取持仓文件失败: {e}")

        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    self.trade_history = json.load(f)
            except Exception:
                pass

    def _rebuild_counts_cache(self):
        self.buy_counts_cache = {}
        self.sell_counts_cache = {} 
        for record in self.trade_history:
            token = record.get('token')
            if not token: continue
            action = record.get('action', '')
            if action == 'BUY':
                self.buy_counts_cache[token] = self.buy_counts_cache.get(token, 0) + 1
            elif 'SELL' in action:
                self.sell_counts_cache[token] = self.sell_counts_cache.get(token, 0) + 1
        logger.info(f"⚡️ 计数缓存已重建 | 历史买入数: {len(self.buy_counts_cache)}")

    def get_token_lock(self, token_mint):
        return self.locks[token_mint]

    def _save_portfolio(self):
        asyncio.get_event_loop().run_in_executor(
            self.calc_executor, self._write_json_worker, PORTFOLIO_FILE, self.portfolio
        )

    def _save_history(self):
        history_snapshot = list(self.trade_history) 
        asyncio.get_event_loop().run_in_executor(
            self.calc_executor, self._write_json_worker, HISTORY_FILE, history_snapshot
        )

    @staticmethod
    def _write_json_worker(filepath, data):
        try:
            temp_file = filepath + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            os.replace(temp_file, filepath)
        except Exception as e:
            logger.error(f"❌ 后台写入文件失败 {filepath}: {e}")

    def _record_history(self, action, token, amount, value_sol):
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "token": token,
            "amount": amount,
            "value_sol": value_sol
        }
        self.trade_history.append(record)
        self._save_history()

    def add_position(self, token_mint, amount_bought, cost_sol):
        if token_mint not in self.portfolio:
            self.portfolio[token_mint] = {'my_balance': 0, 'cost_sol': 0}

        self.portfolio[token_mint]['my_balance'] += amount_bought
        self.portfolio[token_mint]['cost_sol'] += cost_sol
        self.buy_counts_cache[token_mint] = self.buy_counts_cache.get(token_mint, 0) + 1
        
        self._save_portfolio()
        self._record_history("BUY", token_mint, amount_bought, cost_sol)
        logger.info(f"📝 [记账] 新增持仓 {token_mint[:6]}... | 数量: {self.portfolio[token_mint]['my_balance']}")

    def get_buy_counts(self, token_mint):
        return self.buy_counts_cache.get(token_mint, 0)
        
    def get_sell_counts(self, token_mint):
        return self.sell_counts_cache.get(token_mint, 0)

    async def execute_proportional_sell(self, token_mint, smart_money_sold_amt):
        if token_mint not in self.portfolio or self.portfolio[token_mint]['my_balance'] <= 0:
            return

        logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 计算策略...")

        # 🔥 初始化变量
        is_force_clear = False
        reason_msg = ""

        smart_money_remaining = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
        total_before_sell = smart_money_sold_amt + smart_money_remaining

        sell_ratio = 1.0
        if total_before_sell > 0:
            sell_ratio = smart_money_sold_amt / total_before_sell
            # 策略 A：90% 阈值清仓
            if sell_ratio > 0.90: 
                is_force_clear = True
                sell_ratio = 1.0
                reason_msg = f"(卖出比例 {sell_ratio:.1%} > 90% -> 触发清仓)"

        # 策略 B：回合制
        total_buys = self.get_buy_counts(token_mint)
        current_sell_seq = self.get_sell_counts(token_mint) + 1 
        is_tiny_sell = sell_ratio < 0.05 
        
        if not is_force_clear:
            if current_sell_seq >= total_buys and not is_tiny_sell and total_buys > 0:
                logger.warning(f"🚨 [策略触发] 第 {current_sell_seq}/{total_buys} 次卖出 -> 尾单清仓")
                is_force_clear = True
                reason_msg = f"(第 {current_sell_seq} 次 - 尾单清仓)"
            elif current_sell_seq >= total_buys + 2 and total_buys > 0:
                logger.warning(f"🚨 [策略触发] 卖出次数过多 -> 强制止损清仓")
                is_force_clear = True
                reason_msg = f"(第 {current_sell_seq} 次 - 超限清仓)"
            elif current_sell_seq >= total_buys and is_tiny_sell:
                logger.info(f"🛡️ [策略豁免] 次数已满但仅卖出 {sell_ratio:.1%} (试盘) -> 仅跟随")
                reason_msg = f"(第 {current_sell_seq} 次 - 试盘跟随)"

        my_holdings = self.portfolio[token_mint]['my_balance']
        amount_to_sell = 0

        if is_force_clear:
            amount_to_sell = my_holdings
            sell_ratio = 1.0 
        else:
            amount_to_sell = int(my_holdings * sell_ratio)

        if amount_to_sell < 100: return

        # Gas 保护
        async with aiohttp.ClientSession() as session:
            quote = await self.trader.get_quote(session, token_mint, self.trader.SOL_MINT, amount_to_sell)
            if quote:
                est_val_sol = int(quote['outAmount']) / 10**9
                if est_val_sol < 0.01:
                    logger.warning(f"📉 [卖出忽略] 价值仅 {est_val_sol:.4f} SOL -> 省Gas跳过")
                    return
            else:
                return

        # 执行
        logger.info(f"📉 跟随卖出{reason_msg}: {amount_to_sell} (占比 {sell_ratio:.2%})")
        success, est_sol_out = await self.trader.execute_swap(token_mint, self.trader.SOL_MINT, amount_to_sell, SLIPPAGE_SELL)

        if success:
            self.portfolio[token_mint]['my_balance'] -= amount_to_sell
            self.sell_counts_cache[token_mint] = self.sell_counts_cache.get(token_mint, 0) + 1

            if self.portfolio[token_mint]['my_balance'] < 100:
                del self.portfolio[token_mint]
                logger.info(f"✅ {token_mint[:6]}... 已清仓")
                asyncio.create_task(self.trader.close_token_account(token_mint))

            self._save_portfolio()
            self._record_history("SELL", token_mint, amount_to_sell, est_sol_out)
            msg = f"聪明钱跟随卖出。\n代币: {token_mint}\n说明: {reason_msg if reason_msg else '比例跟随'}"
            asyncio.create_task(send_email_async(f"📉 卖出通知: {token_mint[:6]}...", msg))

    async def monitor_sync_positions(self):
        logger.info("🛡️ 持仓同步防断网线程已启动...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue

                for token_mint in list(self.portfolio.keys()):
                    try:
                        my_data = self.portfolio[token_mint]
                        if my_data['my_balance'] <= 0: continue
                        
                        sm_amount_raw = await self.trader.get_token_balance_raw(TARGET_WALLET, token_mint)
                        if sm_amount_raw == 0:
                            logger.warning(f"🛡️ 触发防断网机制：大佬余额归零 -> 强制清仓 {token_mint}")
                            await self.force_sell_all(token_mint, my_data['my_balance'], -0.99)
                    except Exception:
                        pass
                await asyncio.sleep(20)

    async def monitor_1000x_profit(self):
        logger.info("💰 收益监控线程已启动...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue
                
                for token_mint in list(self.portfolio.keys()):
                    try:
                        data = self.portfolio[token_mint]
                        if data['my_balance'] <= 0: continue
                        
                        quote = await self.trader.get_quote(session, token_mint, self.trader.SOL_MINT, data['my_balance'])
                        if quote:
                            curr_val = int(quote['outAmount'])
                            cost = data['cost_sol']
                            roi = (curr_val / cost) - 1 if cost > 0 else 0
                            
                            if roi >= TAKE_PROFIT_ROI:
                                # 🔥 使用配置的比例进行卖出
                                sell_pct = TAKE_PROFIT_SELL_PCT
                                logger.warning(f"🚀 [暴富时刻] {token_mint} ROI: {roi*100:.0f}%！执行止盈策略...")
                                
                                # 计算卖出数量
                                amount_to_sell = int(data['my_balance'] * sell_pct)
                                
                                # 检查剩余价值是否过低
                                est_val_remaining = (curr_val * (1 - sell_pct)) / 10**9
                                is_clear_all = False
                                
                                if est_val_remaining < 0.01: 
                                    amount_to_sell = data['my_balance']
                                    is_clear_all = True
                                    logger.info("   -> 剩余价值过低，改为全仓止盈")
                                else:
                                    logger.info(f"   -> 锁定 {sell_pct*100:.0f}% 利润，保留 {(1-sell_pct)*100:.0f}% 博更高收益！")

                                success, est_sol_out = await self.trader.execute_swap(
                                    token_mint, self.trader.SOL_MINT, amount_to_sell, SLIPPAGE_SELL
                                )
                                
                                if success:
                                    self.portfolio[token_mint]['my_balance'] -= amount_to_sell
                                    
                                    if is_clear_all or self.portfolio[token_mint]['my_balance'] <= 0:
                                        if token_mint in self.portfolio: del self.portfolio[token_mint]
                                        asyncio.create_task(self.trader.close_token_account(token_mint))

                                    self._save_portfolio()
                                    self._record_history("SELL_PROFIT", token_mint, amount_to_sell, est_sol_out)
                                    
                                    msg = f"🚀 止盈触发！\n代币: {token_mint}\nROI: {roi*100:.1f}%\n动作: {'全仓卖出' if is_clear_all else f'卖出{sell_pct*100:.0f}%，保留火种'}\n到手: {est_sol_out/10**9:.4f} SOL"
                                    asyncio.create_task(send_email_async(f"💰 止盈通知: {token_mint[:6]}...", msg))
                                    
                                    # 冷却防刷
                                    await asyncio.sleep(60)

                    except Exception as e:
                        logger.error(f"盯盘异常: {e}")
                
                await asyncio.sleep(10)

    async def force_sell_all(self, token_mint, amount, roi):
        success, est_sol_out = await self.trader.execute_swap(
            token_mint, self.trader.SOL_MINT, amount, SLIPPAGE_SELL
        )
        if success:
            if token_mint in self.portfolio:
                del self.portfolio[token_mint]
            self.sell_counts_cache[token_mint] = self.sell_counts_cache.get(token_mint, 0) + 1
            
            asyncio.create_task(self.trader.close_token_account(token_mint))
            self._save_portfolio()
            self._record_history("SELL_FORCE", token_mint, amount, est_sol_out)
            
            subject = "🚀 暴富止盈" if roi > 0 else "🛡️ 强制风控"
            msg = f"代币: {token_mint}\n原因: {'止盈强平' if roi > 0 else '防断网/清仓'}"
            asyncio.create_task(send_email_async(subject, msg))

    async def schedule_daily_report(self):
        logger.info(f"📅 日报调度器: {REPORT_HOUR:02d}:{REPORT_MINUTE:02d}")
        while self.is_running:
            now = datetime.now()
            target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
            if now >= target: target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self.send_daily_summary()
            await asyncio.sleep(60)

    @staticmethod
    def _calculate_stats_worker(history_snapshot, yesterday_timestamp):
        temp_holdings = {}
        temp_costs = {}
        daily_profit_sol = 0.0
        total_realized_profit_sol = 0.0
        daily_wins = 0
        daily_losses = 0
        total_wins = 0
        total_losses = 0
        COST_THRESHOLD_FOR_WINRATE = 0.01 

        for record in history_snapshot:
            token = record['token']
            action = record['action']
            amount = record['amount']
            val = record['value_sol']
            try:
                rec_time = datetime.strptime(record['time'], "%Y-%m-%d %H:%M:%S")
            except:
                continue 

            if action == 'BUY':
                temp_holdings[token] = temp_holdings.get(token, 0) + amount
                temp_costs[token] = temp_costs.get(token, 0.0) + val
            elif 'SELL' in action:
                current_holding = temp_holdings.get(token, 0)
                total_cost = temp_costs.get(token, 0.0)
                if current_holding > 0:
                    avg_price = total_cost / current_holding
                    cost_of_this_sell = avg_price * amount
                    pnl = val - cost_of_this_sell
                    total_realized_profit_sol += pnl
                    is_today = rec_time >= yesterday_timestamp
                    if is_today:
                        daily_profit_sol += pnl
                    if cost_of_this_sell > COST_THRESHOLD_FOR_WINRATE:
                        if pnl > 0:
                            total_wins += 1
                            if is_today: daily_wins += 1
                        else:
                            total_losses += 1
                            if is_today: daily_losses += 1
                    temp_holdings[token] = max(0, current_holding - amount)
                    temp_costs[token] = max(0.0, total_cost - cost_of_this_sell)

        return {
            "daily_profit_sol": daily_profit_sol,
            "total_realized_profit_sol": total_realized_profit_sol,
            "daily_wins": daily_wins,
            "daily_losses": daily_losses,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "sell_count": sum(1 for x in history_snapshot if 'SELL' in x['action'])
        }

    async def send_daily_summary(self):
        logger.info("📊 正在生成每日日报...")
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                quote = await self.trader.get_quote(session, self.trader.SOL_MINT, usdc_mint, 1 * 10 ** 9)
                sol_price = float(quote['outAmount']) / 10 ** 6 if quote else 0

                balance_resp = await self.trader.rpc_client.get_balance(self.trader.payer.pubkey())
                sol_balance = balance_resp.value / 10 ** 9

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

                yesterday = datetime.now() - timedelta(days=1)
                history_snapshot = list(self.trade_history)
                loop = asyncio.get_event_loop()
                stats = await loop.run_in_executor(
                    self.calc_executor, 
                    self._calculate_stats_worker, 
                    history_snapshot, 
                    yesterday
                )

                daily_profit_sol = stats["daily_profit_sol"]
                total_realized_profit_sol = stats["total_realized_profit_sol"]
                daily_wins = stats["daily_wins"]
                daily_losses = stats["daily_losses"]
                total_wins = stats["total_wins"]
                total_losses = stats["total_losses"]

                daily_total = daily_wins + daily_losses
                daily_win_rate = (daily_wins / daily_total * 100) if daily_total > 0 else 0.0
                total_valid = total_wins + total_losses
                total_win_rate = (total_wins / total_valid * 100) if total_valid > 0 else 0.0
                total_profit_usd = total_realized_profit_sol * sol_price

                report = f"""
【📅 每日交易与资产报告】
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💰 资产概览:
-------------------
• SOL 价格: ${sol_price:.2f}
• 钱包余额: {sol_balance:.4f} SOL
• 持仓价值: {holdings_val_sol:.4f} SOL
• 总计资产: {total_asset_sol:.4f} SOL (≈ ${total_asset_usd:.2f})

📈 战绩统计 (去灰尘版):
-------------------
• 今日已结盈亏: {'+' if daily_profit_sol >= 0 else ''}{daily_profit_sol:.4f} SOL
• 今日有效胜率: {daily_win_rate:.1f}% ({daily_wins} 胜 / {daily_losses} 负)

🏆 历史累计数据:
-------------------
• 累计已结盈亏: {'+' if total_realized_profit_sol >= 0 else ''}{total_realized_profit_sol:.4f} SOL (≈ ${total_profit_usd:.2f})
• 累计有效胜率: {total_win_rate:.1f}% ({total_wins} 胜 / {total_losses} 负)
• 累计交易笔数: {stats['sell_count']} (含灰尘)

👜 当前持仓明细:
{holdings_details if holdings_details else "(空仓)"}

🤖 机器人状态: 正常运行中 (零阻塞模式)
"""
                await send_email_async("📊 [日报] 资产与盈亏统计", report, attachment_path=PORTFOLIO_FILE)
                logger.info("✅ 日报已发送")

            except Exception as e:
                logger.error(f"生成日报失败: {e}")
