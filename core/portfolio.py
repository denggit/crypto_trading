#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : core/portfolio.py
@Description: 核心资产管理 (支持回合制清仓 + 90% 阈值强平 + 防粉尘优化)
"""
import asyncio
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import aiohttp

# 导入配置和工具（本币 USDC）
from config.settings import TARGET_WALLET, SLIPPAGE_SELL, TAKE_PROFIT_ROI, REPORT_HOUR, REPORT_MINUTE, \
    TAKE_PROFIT_SELL_PCT, STOP_LOSS_PCT, USDC_MINT, USDC_DECIMALS
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

        # 🔥 锁与缓存
        self.locks = defaultdict(asyncio.Lock)  # Token 级细粒度锁
        self.buy_counts_cache = {}  # 买入次数缓存
        self.sell_counts_cache = {}  # 卖出次数缓存

        # 线程池
        self.calc_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="StatsCalc")

        # 初始化加载
        self._ensure_data_dir()
        self._load_data()
        self._rebuild_counts_cache()  # 重建买卖计数

    def _ensure_data_dir(self):
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

    def _load_data(self):
        if os.path.exists(PORTFOLIO_FILE):
            try:
                with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
                    self.portfolio = json.load(f)
                # 兼容旧数据：将 cost_sol 迁移为 cost_usdc（本币已改为 USDC）
                for mint, data in self.portfolio.items():
                    if isinstance(data, dict) and 'cost_sol' in data and 'cost_usdc' not in data:
                        data['cost_usdc'] = data['cost_sol']
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
        """ 🚀 重建买入和卖出的计数缓存 """
        self.buy_counts_cache = {}
        self.sell_counts_cache = {}  # Reset

        for record in self.trade_history:
            token = record.get('token')
            if not token: continue

            action = record.get('action', '')

            if action == 'BUY':
                self.buy_counts_cache[token] = self.buy_counts_cache.get(token, 0) + 1
            elif 'SELL' in action:
                self.sell_counts_cache[token] = self.sell_counts_cache.get(token, 0) + 1

        logger.info(
            f"⚡️ 计数缓存已重建 | 历史买入代币数: {len(self.buy_counts_cache)} | 历史卖出代币数: {len(self.sell_counts_cache)}")

    def get_token_lock(self, token_mint):
        return self.locks[token_mint]

    def _save_portfolio(self):
        # 🔥 修复：传递快照而不是引用，避免并发修改导致的数据不一致
        portfolio_snapshot = dict(self.portfolio)
        # 🔥 修复：使用 asyncio.get_running_loop() 替代 get_event_loop()，兼容 Python 3.10+
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.run_in_executor(
            self.calc_executor, self._write_json_worker, PORTFOLIO_FILE, portfolio_snapshot
        )

    def _save_history(self):
        history_snapshot = list(self.trade_history)
        # 🔥 修复：使用 asyncio.get_running_loop() 替代 get_event_loop()，兼容 Python 3.10+
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.run_in_executor(
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

    def _record_history(self, action, token, amount, value_usdc):
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "token": token,
            "amount": amount,
            "value_usdc": value_usdc
        }
        self.trade_history.append(record)
        self._save_history()

        # core/portfolio.py

    async def sync_real_balance(self, token_mint):
        """
        🔥 [核心修复] 强制从链上同步真实余额
        解决：变基、通缩、高滑点导致的"虚空记账"问题
        """
        my_wallet_address = str(self.trader.payer.pubkey())
        real_balance = await self.trader.get_token_balance_raw(my_wallet_address, token_mint)

        if real_balance is not None:
            async with self.get_token_lock(token_mint):
                if token_mint in self.portfolio:
                    old_balance = self.portfolio[token_mint]['my_balance']
                    diff = real_balance - old_balance

                    # 🔥 修复：处理 old_balance == 0 的情况
                    # 如果账本余额为0但链上有余额，或者偏差超过1%，都需要修正
                    should_correct = False
                    if old_balance == 0:
                        # 如果账本为0但链上有余额，直接修正
                        if real_balance > 0:
                            should_correct = True
                    else:
                        # 如果账本不为0，检查偏差是否超过1%
                        if abs(diff) > (old_balance * 0.01):
                            should_correct = True
                    
                    if should_correct:
                        logger.warning(
                            f"⚖️ [余额修正] {token_mint[:6]}... "
                            f"账本: {old_balance} -> 链上: {real_balance} | "
                            f"修正差额: {diff}"
                        )

                        # 1. 更新当前持仓
                        self.portfolio[token_mint]['my_balance'] = real_balance
                        self._save_portfolio()

                        # 2. 🔥🔥🔥 [新增] 同步修正历史记录，防止日报数据错乱 🔥🔥🔥
                        if diff < 0:
                            # 如果币变少了（滑点/税），记为一笔"0收入的卖出"
                            # 这样统计程序就会把这部分成本算作亏损（Realized Loss），账也就平了
                            amount_lost = abs(diff)
                            self._record_history("SELL_CORRECTION", token_mint, amount_lost, 0.0)
                            logger.info(f"📉 [历史修正] 已记录 {amount_lost} 个代币的损耗 (滑点/税)")

                        elif diff > 0:
                            # 如果币变多了（极少见，可能是分红/空投），记为一笔"0成本的买入"
                            self._record_history("BUY", token_mint, diff, 0.0)
                            logger.info(f"📈 [历史修正] 已记录 {diff} 个代币的增量")
    
    async def add_position(self, token_mint, amount_bought, cost_usdc):
        """
        添加持仓记录（本币 USDC）

        Args:
            token_mint: 代币地址
            amount_bought: 买入数量（原始单位）
            cost_usdc: 成本（USDC）
        """
        if amount_bought <= 0 or cost_usdc < 0:
            logger.error(f"❌ [输入验证失败] {token_mint[:6]}... 买入数量: {amount_bought}, 成本: {cost_usdc}")
            return

        async with self.get_token_lock(token_mint):
            if token_mint not in self.portfolio:
                self.portfolio[token_mint] = {'my_balance': 0, 'cost_usdc': 0}

            self.portfolio[token_mint]['my_balance'] += amount_bought
            self.portfolio[token_mint]['cost_usdc'] = self.portfolio[token_mint].get('cost_usdc', self.portfolio[token_mint].get('cost_sol', 0)) + cost_usdc
            self.portfolio[token_mint]['last_buy_time'] = time.time()

            self.buy_counts_cache[token_mint] = self.buy_counts_cache.get(token_mint, 0) + 1

            self._save_portfolio()
            self._record_history("BUY", token_mint, amount_bought, cost_usdc)
            logger.info(
                f"📝 [记账] 新增持仓 {token_mint[:6]}... | 数量: {self.portfolio[token_mint]['my_balance']} | 第 {self.buy_counts_cache[token_mint]} 次买入")

    def get_buy_counts(self, token_mint):
        """
        获取指定代币的累计买入次数
        注意：买入次数不会在清仓后清零，是累计的
        :param token_mint: 代币地址
        :return: 累计买入次数
        """
        return self.buy_counts_cache.get(token_mint, 0)

    def get_sell_counts(self, token_mint):
        return self.sell_counts_cache.get(token_mint, 0)

    def get_position_cost(self, token_mint):
        """
        获取当前代币的总投入成本（本币 USDC）
        注意：成本在按比例卖出时会按比例减少，完全清仓后归零。
        :param token_mint: 代币地址
        :return: 当前持仓的总投入成本（USDC）
        """
        if token_mint in self.portfolio:
            return self.portfolio[token_mint].get('cost_usdc', self.portfolio[token_mint].get('cost_sol', 0.0))
        return 0.0

    def _generate_trade_history_table(self, token_mint):
        """
        生成指定代币的交易历史表格
        :param token_mint: 代币地址
        :return: 交易历史表格文本
        """
        # 筛选该代币的所有交易记录
        token_trades = [r for r in self.trade_history if r.get('token') == token_mint]
        if not token_trades:
            return "暂无交易记录"
        
        # 按时间排序
        token_trades.sort(key=lambda x: x.get('time', ''))
        
        # 计算累计持仓和成本
        current_holding = 0
        total_cost = 0.0
        table_lines = []
        table_lines.append("=" * 100)
        table_lines.append(f"{'时间':<20} {'交易方式':<12} {'数量':<20} {'成本(USDC)':<15} {'盈利情况':<20} {'剩余仓位':<15}")
        table_lines.append("=" * 100)
        
        for record in token_trades:
            time_str = record.get('time', '')
            action = record.get('action', '')
            amount = record.get('amount', 0)
            value_usdc = record.get('value_usdc', record.get('value_sol', 0))
            
            # 简化代币地址显示
            token_short = f"{token_mint[:6]}...{token_mint[-4:]}"
            
            # 交易方式
            if action == 'BUY':
                trade_type = "买入"
                current_holding += amount
                total_cost += value_usdc
                profit_info = "-"
                remaining = current_holding
            elif 'SELL' in action:
                trade_type = "卖出"
                if current_holding > 0:
                    avg_cost = total_cost / current_holding if current_holding > 0 else 0
                    cost_of_sell = avg_cost * amount
                    profit = value_usdc - cost_of_sell
                    profit_pct = (profit / cost_of_sell * 100) if cost_of_sell > 0 else 0
                    profit_info = f"{profit:+.2f} USDC ({profit_pct:+.1f}%)"
                    current_holding -= amount
                    total_cost = max(0, total_cost - cost_of_sell)
                else:
                    profit_info = "N/A"
                    current_holding = 0
                remaining = current_holding
            else:
                trade_type = action
                profit_info = "-"
                remaining = current_holding
            
            # 格式化数量显示
            if amount >= 1e9:
                amount_str = f"{amount / 1e9:.4f}"
            elif amount >= 1e6:
                amount_str = f"{amount / 1e6:.2f}M"
            else:
                amount_str = f"{amount:.0f}"
            
            # 格式化剩余仓位
            if remaining >= 1e9:
                remaining_str = f"{remaining / 1e9:.4f}"
            elif remaining >= 1e6:
                remaining_str = f"{remaining / 1e6:.2f}M"
            else:
                remaining_str = f"{remaining:.0f}"
            
            table_lines.append(
                f"{time_str:<20} {trade_type:<12} {amount_str:<20} {value_usdc:<15.2f} {profit_info:<20} {remaining_str:<15}"
            )
        
        table_lines.append("=" * 100)
        
        # 添加总结信息
        if current_holding > 0:
            table_lines.append(f"\n当前剩余仓位: {current_holding}")
            table_lines.append(f"累计成本: {total_cost:.2f} USDC")
        else:
            table_lines.append(f"\n已全部清仓")
            table_lines.append(f"累计成本: {total_cost:.2f} USDC")
        
        return "\n".join(table_lines)

    async def execute_proportional_sell(self, token_mint, smart_money_sold_amt):
        # 🔥 修复：检查卖出数量是否有效
        if smart_money_sold_amt is None or smart_money_sold_amt <= 0:
            logger.warning(f"⚠️ [卖出跳过] {token_mint[:6]}... 卖出数量无效: {smart_money_sold_amt}")
            return

        # 🔥 修复：在锁保护下检查持仓
        async with self.get_token_lock(token_mint):
            # 1. 检查持仓
            if token_mint not in self.portfolio or self.portfolio[token_mint]['my_balance'] <= 0:
                return
            
            my_holdings = self.portfolio[token_mint]['my_balance']
            total_buys = self.get_buy_counts(token_mint)
            current_sell_seq = self.get_sell_counts(token_mint) + 1

        logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 正在计算策略...")

        # 🔥 初始化变量 (放到最前面！)
        is_force_clear = False
        reason_msg = ""

        # 2. 先把卖出比例算出来
        smart_money_remaining = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
        # 🔥 修复：处理 smart_money_remaining 为 None 或异常的情况
        if smart_money_remaining is None:
            smart_money_remaining = 0.0
        
        total_before_sell = smart_money_sold_amt + smart_money_remaining

        sell_ratio = 1.0
        original_sell_ratio = 0.0  # 🔥 修复：保存原始卖出比例用于日志
        if total_before_sell > 0:
            sell_ratio = smart_money_sold_amt / total_before_sell
            original_sell_ratio = sell_ratio  # 保存原始比例

            # 🔥 策略 A：90% 阈值清仓 (直接修改 is_force_clear)
            if sell_ratio > 0.90:
                is_force_clear = True
                sell_ratio = 1.0
                reason_msg = f"(卖出比例 {original_sell_ratio:.1%} > 90% -> 触发清仓)"
        else:
            # 🔥 修复：如果 total_before_sell == 0，说明大佬已经完全清仓，我们也应该清仓
            logger.warning(f"⚠️ [卖出判断] {token_mint[:6]}... 大佬总持仓为0，触发清仓")
            is_force_clear = True
            sell_ratio = 1.0
            reason_msg = "(大佬总持仓为0 -> 触发清仓)"

        # 3. 策略 B：回合制 + 试盘过滤
        is_tiny_sell = sell_ratio < 0.05

        # 只有当还没有触发清仓时，才去检查回合制逻辑
        if not is_force_clear:
            # 逻辑 B1: 正常清仓 (次数到了，且不是试盘)
            if current_sell_seq >= total_buys and not is_tiny_sell and total_buys > 0:
                logger.warning(
                    f"🚨 [策略触发] 第 {current_sell_seq}/{total_buys} 次卖出 (比例{sell_ratio:.1%}) -> 触发尾单清仓！")
                is_force_clear = True
                reason_msg = f"(第 {current_sell_seq}/{total_buys} 次 - 尾单清仓)"

            # 逻辑 B2: 兜底清仓
            elif current_sell_seq >= total_buys + 2 and total_buys > 0:
                logger.warning(f"🚨 [策略触发] 卖出次数过多 ({current_sell_seq} > {total_buys}+2) -> 触发强制止损清仓！")
                is_force_clear = True
                reason_msg = f"(第 {current_sell_seq} 次 - 超限清仓)"

            # 逻辑 B3: 试盘豁免
            elif current_sell_seq >= total_buys and is_tiny_sell:
                logger.info(f"🛡️ [策略豁免] 虽次数已满，但大哥仅卖出 {sell_ratio:.1%} (试盘) -> 仅跟随，不清仓")
                reason_msg = f"(第 {current_sell_seq} 次 - 试盘跟随)"

        # 4. 计算最终卖出数量
        # 🔥 修复：在锁保护下重新获取持仓（可能已被其他线程修改）
        async with self.get_token_lock(token_mint):
            if token_mint not in self.portfolio:
                logger.warning(f"⚠️ [卖出跳过] {token_mint[:6]}... 持仓已被清仓")
                return
            
            my_holdings = self.portfolio[token_mint]['my_balance']
            amount_to_sell = 0

            if is_force_clear:
                # 强制清仓模式 (整数操作，无浮点误差)
                amount_to_sell = my_holdings
                sell_ratio = 1.0
            else:
                # 正常比例跟单模式 (含试盘跟随)
                amount_to_sell = int(my_holdings * sell_ratio)

            if amount_to_sell < 100:
                return

        # 防粉尘卖出：预计价值过低则跳过（本币 USDC）
        async with aiohttp.ClientSession() as session:
            quote = await self.trader.get_quote(
                session, token_mint, self.trader.QUOTE_MINT, amount_to_sell
            )

            if quote:
                est_val_usdc = int(quote['outAmount']) / (10 ** USDC_DECIMALS)
                if est_val_usdc < 1.0:
                    logger.warning(
                        f"📉 [卖出忽略] 比例虽为 {sell_ratio:.1%}，但预计价值仅 {est_val_usdc:.2f} USDC (< 1) -> 跳过以节省 Gas")
                    return
            else:
                logger.warning(f"⚠️ [卖出跳过] 无法获取 {token_mint} 报价，暂停跟随")
                return

        logger.info(f"📉 跟随卖出{reason_msg}: {amount_to_sell} (占持仓 {sell_ratio:.2%})")
        success, est_out_raw = await self.trader.execute_swap(
            input_mint=token_mint,
            output_mint=self.trader.QUOTE_MINT,
            amount_lamports=amount_to_sell,
            slippage_bps=SLIPPAGE_SELL
        )

        if success:
            est_usdc_out = est_out_raw / (10 ** USDC_DECIMALS)
            async with self.get_token_lock(token_mint):
                if token_mint not in self.portfolio:
                    logger.warning(f"⚠️ [卖出跳过] {token_mint[:6]}... 持仓已被清仓")
                    self._record_history("SELL", token_mint, amount_to_sell, est_usdc_out)
                    return
                my_holdings_before = self.portfolio[token_mint]['my_balance']
                cost_before = self.portfolio[token_mint].get('cost_usdc', self.portfolio[token_mint].get('cost_sol', 0))
            if my_holdings_before > 0:
                sell_ratio = amount_to_sell / my_holdings_before
                self.portfolio[token_mint]['my_balance'] -= amount_to_sell
                cost_reduction = cost_before * sell_ratio
                self.portfolio[token_mint]['cost_usdc'] = max(0, cost_before - cost_reduction)
                logger.info(
                    f"📉 [跟卖记账] {token_mint[:6]}... 卖出 {sell_ratio:.1%} | "
                    f"余额: {my_holdings_before} -> {self.portfolio[token_mint]['my_balance']} | "
                    f"成本: {cost_before:.2f} -> {self.portfolio[token_mint]['cost_usdc']:.2f} USDC"
                )
            else:
                logger.warning(f"⚠️ [异常] {token_mint[:6]}... 卖出时余额异常 ({my_holdings_before})，直接清仓")
                if token_mint in self.portfolio:
                    del self.portfolio[token_mint]
                self._save_portfolio()
                self._record_history("SELL", token_mint, amount_to_sell, est_usdc_out)
                return

            # 更新卖出计数缓存
            self.sell_counts_cache[token_mint] = self.sell_counts_cache.get(token_mint, 0) + 1

            # 🛡️ 只有在完全清仓时，才删除记录（成本归零）
            # 检查当前剩余持仓是否低于粉尘阈值 (100)
            remaining_balance = self.portfolio[token_mint]['my_balance']
            if remaining_balance < 100:
                del self.portfolio[token_mint]
                if token_mint in self.sell_counts_cache:
                    del self.sell_counts_cache[token_mint]
                if token_mint in self.buy_counts_cache:
                    del self.buy_counts_cache[token_mint]
                logger.info(f"✅ {token_mint[:6]}... 已清仓完毕（成本已归零）")
                logger.info(f"🧹 正在尝试回收账户租金...")
                await asyncio.sleep(2)
                async def safe_close_account():
                    try:
                        await self.trader.close_token_account(token_mint)
                    except Exception as e:
                        logger.error(f"⚠️ 关闭账户失败: {e}")
                asyncio.create_task(safe_close_account())
                try:
                    token_trades = [r for r in self.trade_history if r.get('token') == token_mint]
                    total_buy_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if r['action'] == 'BUY')
                    total_sell_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if 'SELL' in r['action'])
                    net_profit = total_sell_usdc - total_buy_usdc
                    roi = (net_profit / total_buy_usdc * 100) if total_buy_usdc > 0 else 0
                    if net_profit > 0:
                        status_icon = "🚀"
                        status_text = "止盈离场 (Win)"
                    else:
                        status_icon = "💸"
                        status_text = "止损割肉 (Loss)"
                    subject = f"{status_icon} 【清仓报告】{token_mint[:4]}... 结盈: {net_profit:+.2f} USDC ({roi:+.1f}%)"
                    trade_table = self._generate_trade_history_table(token_mint)
                    msg = f"""
========================================
       🤖 SmartFlow 交易结案报告
========================================

代币地址: {token_mint}
交易结果: {status_text}

📊 【最终财务统计】
----------------------------------------
💰 总投入本金:  {total_buy_usdc:.2f} USDC
💵 总回收资金:  {total_sell_usdc:.2f} USDC
----------------------------------------
🔥 净利润 (PnL): {net_profit:+.2f} USDC
📈 投资回报率:  {roi:+.2f}%

📝 【完整操作复盘】
{trade_table}

(本邮件由 SmartFlow 自动生成，账户已自动关闭)
"""
                    # 异步发送
                    async def safe_send_email():
                        try:
                            await send_email_async(subject, msg)
                        except Exception as e:
                            logger.error(f"⚠️ 邮件发送失败: {e}")
                    asyncio.create_task(safe_send_email())
                    
                except Exception as e:
                    logger.error(f"构建清仓邮件失败: {e}")

            else:
                logger.info(f"📉 [分批卖出] 剩余持仓 {remaining_balance} (未清仓，不发邮件)")
            self._save_portfolio()
            self._record_history("SELL", token_mint, amount_to_sell, est_usdc_out)

    async def monitor_sync_positions(self):
        """
        持仓同步防断网监控线程
        
        功能：
        - 每20秒检查一次持仓
        - 检测大佬是否已清仓或余额过低
        - 如果检测到异常，触发强制清仓
        
        防护机制：
        - 买入后60秒内跳过检查，避免链上数据同步延迟导致的误判
        - 如果获取余额失败，跳过本次检查（网络波动）
        """
        # 🔥 买入后保护时间（秒）：避免链上数据同步延迟导致的误判
        BUY_PROTECTION_TIME = 60
        
        logger.info("🛡️ 持仓同步防断网线程已启动 (每20秒检查一次)...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue

                current_time = time.time()
                
                for token_mint in list(self.portfolio.keys()):
                    # 🔥🔥🔥 新增锁保护 🔥🔥🔥
                    async with self.get_token_lock(token_mint):
                        try:
                            my_data = self.portfolio[token_mint]
                            if my_data['my_balance'] <= 0: 
                                continue
    
                            # 🔥 新增：买入后保护期检查，避免链上数据同步延迟导致的误判
                            last_buy_time = my_data.get('last_buy_time', 0)
                            if last_buy_time > 0:
                                time_since_buy = current_time - last_buy_time
                                if time_since_buy < BUY_PROTECTION_TIME:
                                    remaining_protection = BUY_PROTECTION_TIME - time_since_buy
                                    logger.debug(
                                        f"🛡️ [保护期] {token_mint[:6]}... 买入后 {time_since_buy:.1f} 秒，"
                                        f"剩余保护时间 {remaining_protection:.1f} 秒，跳过检查"
                                    )
                                    continue
    
                            sm_amount_raw = await self.trader.get_token_balance_raw(TARGET_WALLET, token_mint)
    
                            # 🔥 新增保护：如果获取失败(None)，认为是网络问题，直接跳过本次检查
                            if sm_amount_raw is None:
                                logger.warning(f"⚠️ [同步跳过] 无法获取大佬 {token_mint} 余额 (网络波动)")
                                continue
    
                            should_sell = False
                            reason = ""
    
                            if sm_amount_raw == 0:
                                # 🔥 新增：即使检测到余额为0，也要再次确认（避免误判）
                                # 等待2秒后再次检查，如果还是0，才触发清仓
                                await asyncio.sleep(2)
                                sm_amount_raw_retry = await self.trader.get_token_balance_raw(TARGET_WALLET, token_mint)
                                if sm_amount_raw_retry is not None and sm_amount_raw_retry == 0:
                                    should_sell = True
                                    reason = "大佬余额为 0 (已二次确认)"
                                else:
                                    logger.info(
                                        f"✅ [误判恢复] {token_mint[:6]}... 首次检测为0，二次确认后余额: {sm_amount_raw_retry}"
                                    )
                            else:
                                quote = await self.trader.get_quote(session, token_mint, self.trader.QUOTE_MINT,
                                                                    sm_amount_raw)
                                if quote:
                                    val_in_usdc = int(quote['outAmount']) / (10 ** USDC_DECIMALS)
                                    if val_in_usdc < 5.0:
                                        should_sell = True
                                        reason = f"大佬余额价值仅 {val_in_usdc:.2f} USDC (判定为粉尘)"
    
                            if should_sell:
                                logger.warning(f"😱 发现异常！持有 {token_mint[:6]}... | 原因: {reason}")
                                logger.warning(f"🛡️ 触发防断网机制：立即强制清仓！")
                                await self.force_sell_all(token_mint, my_data['my_balance'], -0.99)
    
                        except Exception as e:
                            logger.error(f"同步检查异常: {e}")

                await asyncio.sleep(20)

    async def monitor_1000x_profit(self):
        logger.info("💰 收益监控线程已启动...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue

                # 复制一份 key 列表防止遍历时修改字典报错
                for token_mint in list(self.portfolio.keys()):
                    # 🔥🔥🔥 新增锁保护 🔥🔥🔥
                    async with self.get_token_lock(token_mint):
                        try:
                            # 再次检查 key 是否存在 (因为可能刚被清仓线程删了)
                            if token_mint not in self.portfolio: continue
                                
                            data = self.portfolio[token_mint]
                            if data['my_balance'] <= 0: continue

                            # 🔥 2. [核心] 先同步真实余额！(净值法的第一步)
                            # 如果这里不查，遇到通缩币就会算错
                            try:
                                # 复用刚才写的同步方法
                                await self.sync_real_balance(token_mint)
                                # 🔥 修复：同步后再次检查 token_mint 是否还存在（可能被清仓线程删除）
                                if token_mint not in self.portfolio:
                                    continue
                                # 刷新一下 data 里的余额 (因为 sync_real_balance 可能改了它)
                                data = self.portfolio[token_mint]
                            except Exception as e:
                                logger.warning(f"⚠️ 同步余额失败 {token_mint}: {e}")
                                # 🔥 修复：同步失败后也要检查 token_mint 是否还存在
                                if token_mint not in self.portfolio:
                                    continue
    
                            quote = await self.trader.get_quote(session, token_mint, self.trader.QUOTE_MINT,
                                                                data['my_balance'])
                            if quote:
                                curr_val_raw = int(quote['outAmount'])
                                curr_val_usdc = curr_val_raw / (10 ** USDC_DECIMALS)
                                cost_usdc = data.get('cost_usdc', data.get('cost_sol', 0))
                                roi = (curr_val_usdc / cost_usdc) - 1 if cost_usdc > 0 else 0
                                if roi >= TAKE_PROFIT_ROI:
                                    logger.warning(
                                        f"🚀 [暴富时刻] {token_mint} 收益率达到 {roi * 100:.0f}%！执行“留种”止盈策略...")
                                    amount_to_sell = int(data['my_balance'] * TAKE_PROFIT_SELL_PCT)
                                    remaining_ratio = 1 - TAKE_PROFIT_SELL_PCT
                                    est_val_remaining = (curr_val_raw * remaining_ratio) / (10 ** USDC_DECIMALS)
                                    is_clear_all = False
                                    if est_val_remaining < 1.0:  # 剩余价值过低，全清
                                        amount_to_sell = data['my_balance']
                                        is_clear_all = True
                                        logger.info("   -> 剩余价值过低，执行全仓止盈")
                                    else:
                                        logger.info(
                                            f"   -> 锁定 {TAKE_PROFIT_SELL_PCT * 100}% 利润，保留 {(1 - TAKE_PROFIT_SELL_PCT) * 100}% 博百倍金狗！")
    
                                    success, est_out_raw = await self.trader.execute_swap(
                                        input_mint=token_mint,
                                        output_mint=self.trader.QUOTE_MINT,
                                        amount_lamports=amount_to_sell,
                                        slippage_bps=SLIPPAGE_SELL
                                    )
                                    if success:
                                        est_usdc_out = est_out_raw / (10 ** USDC_DECIMALS)
                                        my_holdings_before = self.portfolio[token_mint]['my_balance']
                                        remaining_balance = my_holdings_before - amount_to_sell
                                        if my_holdings_before > 0:
                                            self.portfolio[token_mint]['my_balance'] -= amount_to_sell
                                            logger.info(
                                                f"💰 [止盈记账] {token_mint[:6]}... 卖出部分止盈 | "
                                                f"余额: {my_holdings_before} -> {self.portfolio[token_mint]['my_balance']} | "
                                                f"成本保持: {self.portfolio[token_mint].get('cost_usdc', 0):.2f} USDC (用于追踪原始投入)"
                                            )
                                        else:
                                            logger.warning(f"⚠️ [异常] {token_mint[:6]}... 止盈卖出时余额异常 ({my_holdings_before})，直接清仓")
                                            if token_mint in self.portfolio:
                                                del self.portfolio[token_mint]
                                            self._save_portfolio()
                                            self._record_history("SELL_PROFIT", token_mint, amount_to_sell, est_usdc_out)
                                            return
                                        if is_clear_all or self.portfolio[token_mint]['my_balance'] <= 0:
                                            if token_mint in self.portfolio:
                                                del self.portfolio[token_mint]
                                            remaining_balance = 0
                                            async def safe_close_account():
                                                try:
                                                    await self.trader.close_token_account(token_mint)
                                                except Exception as e:
                                                    logger.error(f"⚠️ 关闭账户失败: {e}")
                                            asyncio.create_task(safe_close_account())
                                        self._save_portfolio()
                                        self._record_history("SELL_PROFIT", token_mint, amount_to_sell, est_usdc_out)
                                        try:
                                            total_cost = data.get('cost_usdc', data.get('cost_sol', 0))
                                            cost_of_this_sell = total_cost * (amount_to_sell / my_holdings_before) if my_holdings_before > 0 else 0.0
                                            realized_profit = est_usdc_out - cost_of_this_sell
                                            val_remaining_usdc = est_val_remaining
                                            sell_pct = TAKE_PROFIT_SELL_PCT * 100
                                            remain_pct = (1 - TAKE_PROFIT_SELL_PCT) * 100
                                            trade_table = self._generate_trade_history_table(token_mint)
                                            subject = f"🚀 【暴富止盈】{token_mint[:4]}... 锁定利润 {realized_profit:+.2f} USDC"
                                            msg = f"""
    ========================================
           🎉 SmartFlow 止盈锁定报告
    ========================================
    
    代币地址: {token_mint}
    当前涨幅: {roi * 100:.1f}% (触发 1000% 止盈)
    
    💰 【本次锁定 (Pocket)】
    ----------------------------------------
    🔨 卖出比例:  {sell_pct:.0f}%
    💵 到手资金:  {est_usdc_out:.2f} USDC
    🔥 本次净赚:  {realized_profit:+.2f} USDC (已落袋)
    
    💎 【剩余博弈 (Moonbag)】
    ----------------------------------------
    📦 保留仓位:  {remain_pct:.0f}%
    🦄 当前价值:  {val_remaining_usdc:.2f} USDC
    (成本已大幅收回，剩余仓位零风险格局！)
    
    📝 【交易流水】
    {trade_table}
    """
                                            async def safe_send_email():
                                                try:
                                                    await send_email_async(subject, msg)
                                                except Exception as e:
                                                    logger.error(f"⚠️ 邮件发送失败: {e}")
                                            asyncio.create_task(safe_send_email())
    
                                        except Exception as e:
                                            logger.error(f"构建止盈邮件失败: {e}")
    
                                        # 稍微休息一下，防止针对同一个币疯狂触发
                                        await asyncio.sleep(60)
    
                        except Exception as e:
                            logger.error(f"盯盘异常: {e}")

                await asyncio.sleep(10)

    async def monitor_stop_loss(self):
        """
        止损监控线程：监控持仓亏损，当亏损达到止损百分比时触发止损卖出
        
        功能：
        - 每10秒检查一次持仓
        - 计算每个持仓的当前收益率（ROI）
        - 如果 ROI <= -STOP_LOSS_PCT（即亏损达到止损百分比），触发全仓止损卖出
        
        成本计算说明：
        - 多次买入：成本累加（每次买入都会累加成本）
        - 按比例卖出（跟卖）：成本按比例减少（保持成本与持仓的对应关系）
        - 止盈卖出：成本保持不变（用于追踪原始投入）
        - 止损计算：使用剩余成本计算 ROI = (当前价值 / 剩余成本) - 1
        
        示例：
        - 买入3次，每次0.1 SOL：总成本 = 0.3 SOL
        - 跟卖50%：剩余成本 = 0.15 SOL（按比例减少）
        - 止损计算：ROI = (当前价值 / 0.15 SOL) - 1
        
        止损策略：
        - 止损时全仓卖出，不留仓位
        - 止损后发送邮件通知
        """
        logger.info(f"🛡️ 止损监控线程已启动 (止损阈值: {STOP_LOSS_PCT * 100:.0f}%)...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue

                # 复制一份 key 列表防止遍历时修改字典报错
                for token_mint in list(self.portfolio.keys()):
                    # 🔥🔥🔥 新增锁保护 🔥🔥🔥
                    async with self.get_token_lock(token_mint):
                        try:
                            # 🔥 修复：再次检查 key 是否存在（可能被其他线程删除）
                            if token_mint not in self.portfolio:
                                continue
                            
                            data = self.portfolio[token_mint]
                            if data['my_balance'] <= 0: 
                                continue

                            quote = await self.trader.get_quote(
                                session, token_mint, self.trader.QUOTE_MINT, data['my_balance']
                            )
                            if quote:
                                curr_val_usdc = int(quote['outAmount']) / (10 ** USDC_DECIMALS)
                                cost_usdc = data.get('cost_usdc', data.get('cost_sol', 0))
                                my_balance = data['my_balance']
                                if my_balance <= 0:
                                    continue
                                roi = (curr_val_usdc / cost_usdc) - 1 if cost_usdc > 0 else 0
                                logger.debug(
                                    f"📊 [止损监控] {token_mint[:6]}... | "
                                    f"当前价值: {curr_val_usdc:.2f} USDC | "
                                    f"剩余成本: {cost_usdc:.2f} USDC | "
                                    f"剩余余额: {my_balance} | "
                                    f"当前ROI: {roi * 100:.1f}%"
                                )
                                if roi <= -STOP_LOSS_PCT:
                                    logger.warning(
                                        f"🛑 [止损触发] {token_mint[:6]}... 亏损达到 {roi * 100:.1f}% "
                                        f"(止损阈值: {STOP_LOSS_PCT * 100:.0f}%)！执行全仓止损卖出...")
                                    amount_to_sell = data['my_balance']
                                    success, est_out_raw = await self.trader.execute_swap(
                                        input_mint=token_mint,
                                        output_mint=self.trader.QUOTE_MINT,
                                        amount_lamports=amount_to_sell,
                                        slippage_bps=SLIPPAGE_SELL
                                    )
                                    if success:
                                        est_usdc_out = est_out_raw / (10 ** USDC_DECIMALS)
                                        my_holdings_before = data['my_balance']
                                        cost_before = cost_usdc
                                        
                                        # 删除持仓记录（成本归零）
                                        if token_mint in self.portfolio:
                                            del self.portfolio[token_mint]
                                        
                                        # 更新卖出计数缓存
                                        self.sell_counts_cache[token_mint] = self.sell_counts_cache.get(token_mint, 0) + 1
                                        
                                        # 重置买入计数（止损后可以重新买入）
                                        if token_mint in self.buy_counts_cache:
                                            del self.buy_counts_cache[token_mint]
                                        
                                        logger.info(
                                            f"🛑 [止损完成] {token_mint[:6]}... 已全仓止损卖出 | "
                                            f"卖出数量: {my_holdings_before} | "
                                            f"成本: {cost_before:.2f} USDC"
                                        )
                                        logger.info(f"🧹 正在尝试回收账户租金...")
                                        await asyncio.sleep(2)
                                        async def safe_close_account():
                                            try:
                                                await self.trader.close_token_account(token_mint)
                                            except Exception as e:
                                                logger.error(f"⚠️ 关闭账户失败: {e}")
                                        asyncio.create_task(safe_close_account())
                                        self._save_portfolio()
                                        self._record_history("SELL_STOP_LOSS", token_mint, amount_to_sell, est_usdc_out)
                                        try:
                                            token_trades = [r for r in self.trade_history if r.get('token') == token_mint]
                                            total_buy_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if r['action'] == 'BUY')
                                            total_sell_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if 'SELL' in r['action'])
                                            net_profit = total_sell_usdc - total_buy_usdc
                                            final_roi = (net_profit / total_buy_usdc * 100) if total_buy_usdc > 0 else 0
                                            trade_table = self._generate_trade_history_table(token_mint)
                                            subject = f"🛑 【止损报告】{token_mint[:4]}... 亏损: {net_profit:+.2f} USDC ({final_roi:+.1f}%)"
                                            msg = f"""
========================================
       🛡️ SmartFlow 止损执行报告
========================================

代币地址: {token_mint}
触发原因: 亏损达到止损阈值 ({STOP_LOSS_PCT * 100:.0f}%)
执行动作: 全仓止损卖出

📊 【最终财务统计】
----------------------------------------
💰 总投入本金:  {total_buy_usdc:.2f} USDC
💵 总回收资金:  {total_sell_usdc:.2f} USDC
----------------------------------------
🔥 净利润 (PnL): {net_profit:+.2f} USDC
📉 最终回报率:  {final_roi:+.2f}%

📝 【完整操作复盘】
{trade_table}

(本邮件由 SmartFlow 自动生成，账户已自动关闭)
"""
                                            async def safe_send_email():
                                                try:
                                                    await send_email_async(subject, msg)
                                                except Exception as e:
                                                    logger.error(f"⚠️ 邮件发送失败: {e}")
                                            asyncio.create_task(safe_send_email())
                                            
                                        except Exception as e:
                                            logger.error(f"构建止损邮件失败: {e}")

                                        # 稍微休息一下，防止针对同一个币疯狂触发
                                        await asyncio.sleep(60)
                                else:
                                    # 未触发止损，记录当前亏损情况（仅调试用）
                                    if roi < 0:
                                        logger.debug(
                                            f"📊 [持仓监控] {token_mint[:6]}... 当前亏损: {roi * 100:.1f}% "
                                            f"(止损阈值: {STOP_LOSS_PCT * 100:.0f}%)"
                                        )

                        except Exception as e:
                            logger.error(f"止损监控异常: {e}")

                await asyncio.sleep(10)

    async def force_sell_all(self, token_mint, amount, roi):
        # 🔥 修复：在锁保护下检查并获取持仓
        async with self.get_token_lock(token_mint):
            if token_mint not in self.portfolio:
                logger.warning(f"⚠️ [强平跳过] {token_mint[:6]}... 持仓不存在")
                return
            
            # 使用账本中的余额作为默认值
            if amount <= 0:
                amount = self.portfolio[token_mint].get('my_balance', 0)
        
        # 🔥 [新增] 在强平前，最后确认一次真实余额
        # 防止传入的 amount 是旧账本数据，导致卖出失败
        try:
            real_balance = await self.trader.get_token_balance_raw(str(self.trader.payer.pubkey()), token_mint)
            if real_balance is not None and real_balance > 0:
                amount = real_balance # 用真实余额覆盖传入的 amount
                logger.info(f"🛡️ [强平修正] 使用链上真实余额: {amount}")
            elif real_balance == 0:
                logger.warning(f"⚠️ [强平取消] 链上余额为 0，无需卖出")
                async with self.get_token_lock(token_mint):
                    if token_mint in self.portfolio:
                        del self.portfolio[token_mint]
                return
        except Exception as e:
            logger.warning(f"⚠️ 强平前同步失败: {e} (将尝试使用账本余额)")
        
        if amount <= 0:
            logger.warning(f"⚠️ [强平跳过] {token_mint[:6]}... 卖出数量为0")
            return
            
        success, est_out_raw = await self.trader.execute_swap(
            input_mint=token_mint,
            output_mint=self.trader.QUOTE_MINT,
            amount_lamports=amount,
            slippage_bps=SLIPPAGE_SELL
        )
        if success:
            est_usdc_out = est_out_raw / (10 ** USDC_DECIMALS)
            async with self.get_token_lock(token_mint):
                if token_mint in self.portfolio:
                    del self.portfolio[token_mint]
                self.sell_counts_cache[token_mint] = self.sell_counts_cache.get(token_mint, 0) + 1
            logger.info(f"🧹 [强平] 正在尝试回收账户租金...")
            await asyncio.sleep(2)
            async def safe_close_account():
                try:
                    await self.trader.close_token_account(token_mint)
                except Exception as e:
                    logger.error(f"⚠️ 关闭账户失败: {e}")
            asyncio.create_task(safe_close_account())
            self._save_portfolio()
            self._record_history("SELL_FORCE", token_mint, amount, est_usdc_out)
            try:
                token_trades = [r for r in self.trade_history if r.get('token') == token_mint]
                total_buy_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if r['action'] == 'BUY')
                total_sell_usdc = sum(r.get('value_usdc', r.get('value_sol', 0)) for r in token_trades if 'SELL' in r['action'])
                net_profit = total_sell_usdc - total_buy_usdc
                final_roi = (net_profit / total_buy_usdc * 100) if total_buy_usdc > 0 else 0
                if roi == -0.99:
                    reason_title = "🛡️ 触发防断网/大哥清仓风控"
                else:
                    reason_title = "⚠️ 触发强制止损/其他风控"
                status_icon = "🚀" if net_profit > 0 else "😭"
                status_text = "盈利离场" if net_profit > 0 else "亏损离场"
                subject = f"{status_icon} 【强平报告】{token_mint[:4]}... 结盈: {net_profit:+.2f} USDC"
                trade_table = self._generate_trade_history_table(token_mint)
                msg = f"""
========================================
       🤖 SmartFlow 风控执行报告
========================================

触发原因: {reason_title}
执行动作: 全仓强制卖出
交易结果: {status_text}

📊 【最终财务统计】
----------------------------------------
💰 总投入本金:  {total_buy_usdc:.2f} USDC
💵 总回收资金:  {total_sell_usdc:.2f} USDC
----------------------------------------
🔥 净利润 (PnL): {net_profit:+.2f} USDC
📉 最终回报率:  {final_roi:+.2f}%

📝 【完整操作复盘】
{trade_table}
"""
                async def safe_send_email():
                    try:
                        await send_email_async(subject, msg)
                    except Exception as e:
                        logger.error(f"⚠️ 邮件发送失败: {e}")
                asyncio.create_task(safe_send_email())
                
            except Exception as e:
                logger.error(f"构建强平邮件失败: {e}")

    async def schedule_daily_report(self):
        """ 每日日报调度器 (支持自定义时间) """
        # 🔥 2. 日志里打印出设定好的时间，方便检查
        logger.info(f"📅 日报调度器已启动 (每天 {REPORT_HOUR:02d}:{REPORT_MINUTE:02d} 发送)...")

        while self.is_running:
            now = datetime.now()

            # 🔥 3. 使用配置的时间变量
            target_time = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)

            # 如果今天的时间已经过了，就定在明天的这个时间
            if now >= target_time:
                target_time += timedelta(days=1)

            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"⏳ 距离发送日报还有 {sleep_seconds / 3600:.1f} 小时")

            await asyncio.sleep(sleep_seconds)
            await self.send_daily_summary()

            # 发送完休息 60 秒，防止一分钟内重复触发
            await asyncio.sleep(60)

    @staticmethod
    def _calculate_stats_worker(history_snapshot, yesterday_timestamp):
        temp_holdings = {}
        temp_costs = {}
        daily_profit_usdc = 0.0
        total_realized_profit_usdc = 0.0
        daily_wins = 0
        daily_losses = 0
        total_wins = 0
        total_losses = 0
        COST_THRESHOLD_FOR_WINRATE = 0.01
        for record in history_snapshot:
            token = record['token']
            action = record['action']
            amount = record['amount']
            val = record.get('value_usdc', record.get('value_sol', 0))
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
                    total_realized_profit_usdc += pnl
                    is_today = rec_time >= yesterday_timestamp
                    if is_today:
                        daily_profit_usdc += pnl
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
            "daily_profit_usdc": daily_profit_usdc,
            "total_realized_profit_usdc": total_realized_profit_usdc,
            "daily_wins": daily_wins,
            "daily_losses": daily_losses,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "sell_count": sum(1 for x in history_snapshot if 'SELL' in x['action'])
        }

    async def send_daily_summary(self):
        """生成每日日报（本币 USDC）。"""
        logger.info("📊 正在生成每日日报...")
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                wallet = str(self.trader.payer.pubkey())
                usdc_balance = await self.trader.get_token_balance(wallet, self.trader.QUOTE_MINT)
                balance_resp = await self.trader.rpc_client.get_balance(self.trader.payer.pubkey())
                sol_balance = balance_resp.value / 10 ** 9

                holdings_val_usdc = 0.0
                holdings_cost_usdc = 0.0
                holding_wins = 0
                holding_losses = 0
                holdings_count = 0
                holdings_details = ""

                if self.portfolio:
                    for mint, data in self.portfolio.items():
                        qty = data['my_balance']
                        cost = data.get('cost_usdc', data.get('cost_sol', 0))
                        if qty > 0:
                            holdings_count += 1
                            q = await self.trader.get_quote(session, mint, self.trader.QUOTE_MINT, qty)
                            if q is None:
                                logger.warning(f"⚠️ 无法获取 {mint[:6]}... 报价，使用成本作为估值")
                                val = cost
                            else:
                                val = int(q['outAmount']) / (10 ** USDC_DECIMALS)
                            holdings_val_usdc += val
                            holdings_cost_usdc += cost
                            pnl = val - cost
                            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                            if pnl > 0:
                                holding_wins += 1
                                icon = "🟢"
                            else:
                                holding_losses += 1
                                icon = "🔴"
                            holdings_details += f"{icon} {mint[:4]}..: {val:.2f} USDC ({pnl_pct:+.1f}%)\n"

                unrealized_pnl_usdc = holdings_val_usdc - holdings_cost_usdc
                total_asset_usdc = usdc_balance + holdings_val_usdc

                yesterday = datetime.now() - timedelta(days=1)
                history_snapshot = list(self.trade_history)
                loop = asyncio.get_event_loop()
                stats = await loop.run_in_executor(
                    self.calc_executor,
                    self._calculate_stats_worker,
                    history_snapshot,
                    yesterday
                )
                total_net_pnl_usdc = stats["total_realized_profit_usdc"] + unrealized_pnl_usdc
                combined_wins = stats["total_wins"] + holding_wins
                combined_losses = stats["total_losses"] + holding_losses
                combined_total = combined_wins + combined_losses
                combined_win_rate = (combined_wins / combined_total * 100) if combined_total > 0 else 0.0

                report = f"""
【📅 每日资产与盈亏全景】(本币 USDC)
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💰 资产总览 (Mark-To-Market):
-------------------
• 钱包 USDC: {usdc_balance:.2f} USDC
• 钱包 SOL (Gas): {sol_balance:.4f} SOL
• 持仓市值: {holdings_val_usdc:.2f} USDC
• 资产总值: {total_asset_usdc:.2f} USDC

📊 盈亏分析 (含持仓):
-------------------
• 历史已结盈亏: {stats['total_realized_profit_usdc']:+.2f} USDC
• 当前浮动盈亏: {unrealized_pnl_usdc:+.2f} USDC
• 账户净盈亏:   {total_net_pnl_usdc:+.2f} USDC 🔥

🏆 综合胜率 (含持仓):
-------------------
• 综合胜率: {combined_win_rate:.1f}% 
  (共 {combined_total} 局: {combined_wins} 胜 / {combined_losses} 负)
  *包含 {stats['sell_count']} 笔历史卖出 + {holdings_count} 个当前持仓

👜 持仓明细 ({holdings_count} 个):
{holdings_details if holdings_details else "(空仓)"}
"""
                await send_email_async("📊 [日报] 资产净值与持仓透视", report, attachment_path=PORTFOLIO_FILE)
                logger.info("✅ 日报已发送")

            except Exception as e:
                logger.error(f"生成日报失败: {e}")
