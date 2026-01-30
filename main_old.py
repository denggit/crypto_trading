#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 4:18 AM
@File       : main.py
@Description: 
"""
# !/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : main.py
@Description: 智能跟单机器人 (集成版 + 邮件通知)
"""
import os

from dotenv import load_dotenv

load_dotenv()  # 显式加载，确保 os.getenv 能读到数据

# 🔥🔥【新增】强制让所有网络请求都走 Clash 代理 (包括 solana_trader.py 和 solana SDK)
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

import asyncio
import json
import logging
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import aiohttp
import websockets

from services.solana_trader import SolanaTrader

# ================= 配置区域 =================
API_KEY = os.getenv("API_KEY")
TARGET_WALLET = os.getenv("TARGET_WALLET")

# 邮箱配置 (从 .env 读取)
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
# SMTP 服务器配置 (默认 Gmail，如果是 QQ 请改为 smtp.qq.com, 端口 465)
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465

# 基础配置
WSS_ENDPOINT = f"wss://mainnet.helius-rpc.com/?api-key={API_KEY}"
HTTP_ENDPOINT = f"https://api.helius.xyz/v0/transactions/?api-key={API_KEY}"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={API_KEY}"

# 策略配置
COPY_AMOUNT_SOL = 0.1
SLIPPAGE_BUY = 1000
SLIPPAGE_SELL = 2000
TAKE_PROFIT_ROI = 10.0

# 风控配置
MIN_LIQUIDITY_USD = 20000
MAX_FDV = 5000000
MIN_FDV = 200000

# ================= 日志配置 =================
log_dir = "log"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
log_filename = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BotMaster")


# ================= 模块：邮件通知系统 =================
def send_email_sync(subject, content):
    """ 同步发送邮件逻辑 """
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logger.warning("⚠️ 邮箱未配置，跳过发送邮件。")
        return

    try:
        message = MIMEText(content, 'plain', 'utf-8')
        message['From'] = Header("Solana Bot", 'utf-8')
        message['To'] = Header("Master", 'utf-8')
        message['Subject'] = Header(subject, 'utf-8')

        # 连接 SMTP 服务器
        if "qq.com" in SMTP_SERVER:
            # QQ 邮箱通常使用 SSL (端口 465)
            server = smtplib.SMTP_SSL(SMTP_SERVER, 465)
        else:
            # Gmail 等通常使用 TLS (端口 587)
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()

        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], message.as_string())
        server.quit()
        logger.info(f"📧 邮件发送成功: {subject}")
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")


async def send_email_async(subject, content):
    """ 异步包装器，防止阻塞主线程 """
    await asyncio.to_thread(send_email_sync, subject, content)


# ================= 修复后的流动性检查模块 =================
async def check_token_liquidity(session, token_mint):
    # 1. 忽略 SOL
    if token_mint == "So11111111111111111111111111111111111111112":
        return True, 999999999, 999999999

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"

    # 🔥 核心修复：添加浏览器伪装头 (User-Agent)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://dexscreener.com/"
    }

    try:
        # 在 get 请求中加入 headers=headers
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get('pairs', [])

                if not pairs:
                    # 只有日志级别为 WARNING 时才打印，防止刷屏，但这里我们需要知道为什么为空
                    # 有时候新币确实没收录，这是正常的风控拦截
                    return False, 0, 0

                # 筛选 Solana 链的池子
                solana_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if not solana_pairs:
                    return False, 0, 0

                # 找最大池子
                best_pair = max(solana_pairs, key=lambda x: x.get('liquidity', {}).get('usd', 0))
                liq = best_pair.get('liquidity', {}).get('usd', 0)
                fdv = best_pair.get('fdv', 0)

                return True, liq, fdv

            elif response.status == 429:
                logger.warning(f"⚠️ DexScreener 限流 (429)，建议稍后重试。")
            else:
                logger.warning(f"⚠️ DexScreener 请求失败: HTTP {response.status}")

    except Exception as e:
        logger.error(f"⚠️ 风控检查报错: {e}")

    # 默认拦截
    return False, 0, 0


# ================= 模块：仓位管理 =================
# main.py 中的 PortfolioManager 类 (更新版)

class PortfolioManager:
    def __init__(self, trader: SolanaTrader):
        self.trader = trader
        self.portfolio = {}
        self.trade_history = []  # 🔥 新增：历史交易记录 (用于日报)
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
        # ... (检查持仓逻辑不变) ...
        if token_mint not in self.portfolio or self.portfolio[token_mint]['my_balance'] <= 0:
            logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 但我未持有，跳过。")
            return

        logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 正在计算比例...")
        smart_money_remaining = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
        total_before_sell = smart_money_sold_amt + smart_money_remaining

        sell_ratio = 1.0
        if total_before_sell > 0:
            sell_ratio = smart_money_sold_amt / total_before_sell
            if sell_ratio > 0.99: sell_ratio = 1.0

        my_holdings = self.portfolio[token_mint]['my_balance']
        amount_to_sell = int(my_holdings * sell_ratio)

        if amount_to_sell < 100: return

        logger.info(f"📉 跟随卖出: {amount_to_sell} (占持仓 {sell_ratio:.2%})")
        success, est_sol_out = await self.trader.execute_swap(
            input_mint=token_mint,
            output_mint=self.trader.SOL_MINT,
            amount_lamports=amount_to_sell,
            slippage_bps=SLIPPAGE_SELL
        )

        if success:
            self.portfolio[token_mint]['my_balance'] -= amount_to_sell

            # 记录历史 (est_sol_out 是大概回来的 SOL 数量)
            self._record_history("SELL", token_mint, amount_to_sell, est_sol_out)

            # 邮件通知
            msg = f"检测到聪明钱卖出，已跟随卖出。\n\n代币: {token_mint}\n数量: {amount_to_sell}\n比例: {sell_ratio:.1%}"
            asyncio.create_task(send_email_async(f"📉 跟随卖出成功: {token_mint[:6]}...", msg))

            if self.portfolio[token_mint]['my_balance'] < 100 and token_mint in self.portfolio:
                del self.portfolio[token_mint]
                logger.info(f"✅ {token_mint[:6]}... 已清仓完毕")

    async def monitor_sync_positions(self):
        # ... (原有防断网逻辑保持不变) ...
        logger.info("🛡️ 持仓同步防断网线程已启动 (每20秒检查一次)...")
        while self.is_running:
            if not self.portfolio:
                await asyncio.sleep(5)
                continue
            for token_mint in list(self.portfolio.keys()):
                try:
                    my_data = self.portfolio[token_mint]
                    if my_data['my_balance'] <= 0: continue
                    sm_balance = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
                    if sm_balance < 1:
                        logger.warning(f"😱 发现异常！持有 {token_mint[:6]}... 但大佬余额为 0！")
                        logger.warning(f"🛡️ 触发防断网机制：立即强制清仓！")
                        await self.force_sell_all(token_mint, my_data['my_balance'], -0.99)
                except Exception as e:
                    logger.error(f"同步检查异常: {e}")
            await asyncio.sleep(20)

    async def monitor_1000x_profit(self):
        # ... (原有止盈逻辑保持不变) ...
        logger.info("💰 收益监控线程已启动...")
        async with aiohttp.ClientSession(trust_env=True) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue
                for token_mint in list(self.portfolio.keys()):
                    try:
                        data = self.portfolio[token_mint]
                        if data['my_balance'] <= 0: continue
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
        success, est_sol_out = await self.trader.execute_swap(
            token_mint, self.trader.SOL_MINT, amount, SLIPPAGE_SELL
        )
        if success:
            # 记录历史
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

    # 🔥🔥🔥【新增】每日日报生成器 🔥🔥🔥
    async def schedule_daily_report(self):
        logger.info("📅 日报调度器已启动 (每天 09:00 发送)...")
        while self.is_running:
            now = datetime.now()
            # 设定目标时间：今天的 09:00:00
            target_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

            # 如果今天已经过了 9点，就定在明天 9点
            if now >= target_time:
                from datetime import timedelta
                target_time += timedelta(days=1)

            # 计算还要睡多久 (秒)
            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"⏳ 距离发送日报还有 {sleep_seconds / 3600:.1f} 小时")

            # 睡觉等待
            await asyncio.sleep(sleep_seconds)

            # 醒来！开始干活
            await self.send_daily_summary()

            # 睡一小会儿防止重复触发
            await asyncio.sleep(60)

    async def send_daily_summary(self):
        logger.info("📊 正在生成每日日报...")
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                # 1. 获取 SOL 价格 (USDC)
                # Jup Quote: 1 SOL -> USDC
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
                            # 询价
                            q = await self.trader.get_quote(session, mint, self.trader.SOL_MINT, qty)
                            val = int(q['outAmount']) / 10 ** 9 if q else 0
                            holdings_val_sol += val
                            holdings_details += f"- {mint[:6]}...: 持有 {qty}, 价值 {val:.2f} SOL\n"

                total_asset_sol = sol_balance + holdings_val_sol
                total_asset_usd = total_asset_sol * sol_price

                # 4. 统计昨日交易
                # 这里简单统计一下 trade_history 里的条数
                buy_count = sum(1 for x in self.trade_history if x['action'] == 'BUY')
                sell_count = sum(1 for x in self.trade_history if 'SELL' in x['action'])

                # 5. 生成报告文本
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
                # 发送
                await send_email_async("📊 [日报] 资产与交易总结", report)

                # (可选) 清空历史记录，或者保留做累计
                # self.trade_history.clear()

            except Exception as e:
                logger.error(f"生成日报失败: {e}")


# ================= 辅助模块：交易解析 =================
async def fetch_transaction_details(session, signature):
    payload = {"transactions": [signature]}
    try:
        async with session.post(HTTP_ENDPOINT, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                if len(data) > 0: return data[0]
    except Exception:
        pass
    return None


def parse_tx(tx_data):
    if not tx_data: return None
    token_transfers = tx_data.get('tokenTransfers', [])
    trade_info = {"action": "UNKNOWN", "token_address": None, "amount": 0}

    out_tokens = []
    in_tokens = []

    # 🚫 黑名单：忽略 SOL, USDC, USDT
    IGNORE_MINTS = [
        "So11111111111111111111111111111111111111112",  # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    ]

    for tx in token_transfers:
        mint = tx['mint']
        if mint in IGNORE_MINTS: continue  # 🔥 遇到稳定币直接跳过

        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, tx['tokenAmount']))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, tx['tokenAmount']))

    # (原本的判断逻辑保持不变...)
    if in_tokens:
        trade_info['action'] = "BUY"
        trade_info['token_address'] = in_tokens[0][0]
        trade_info['amount'] = in_tokens[0][1]
    elif out_tokens:
        trade_info['action'] = "SELL"
        trade_info['token_address'] = out_tokens[0][0]
        trade_info['amount'] = out_tokens[0][1]

    return trade_info


# ================= 核心逻辑：监控任务 =================
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
        if liq < MIN_LIQUIDITY_USD: return
        if fdv < MIN_FDV: return
        if fdv > MAX_FDV: return

        # 2. 执行买入
        logger.info(f"🎯 正在跟单买入: {token}")
        amount_in = int(COPY_AMOUNT_SOL * 10 ** 9)
        success, est_out = await pm.trader.execute_swap(
            pm.trader.SOL_MINT, token, amount_in, SLIPPAGE_BUY
        )
        if success:
            pm.add_position(token, est_out, amount_in)

            # --- 发送买入邮件 ---
            msg = f"成功跟单买入新金狗！\n\n代币: {token}\n池子: ${liq:,.0f}\n市值: ${fdv:,.0f}\n投入: {COPY_AMOUNT_SOL} SOL"
            asyncio.create_task(send_email_async(f"🎯 跟单买入: {token[:6]}...", msg))

    elif trade['action'] == "SELL":
        await pm.execute_proportional_sell(token, trade['amount'])


async def start_monitor(pm: PortfolioManager):
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 连接 WebSocket: {TARGET_WALLET}...")
                async with websockets.connect(WSS_ENDPOINT, ping_interval=30, ping_timeout=60) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                    }))
                    logger.info("👀 监控已就绪...")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if "method" in data and data["method"] == "logsNotification":
                            res = data['params']['result']
                            if any("Swap" in log for log in res['value']['logs']):
                                asyncio.create_task(process_tx_task(session, res['value']['signature'], pm))
            except Exception as e:
                logger.error(f"❌ 连接断开: {e}, 3秒后重连...")
                await asyncio.sleep(3)


# ================= 主程序启动入口 =================
async def main():
    # 1. 初始化交易器
    trader = SolanaTrader(RPC_URL)

    # 2. 初始化仓位管理器
    pm = PortfolioManager(trader)

    # 3. 并发运行所有任务
    await asyncio.gather(
        pm.monitor_1000x_profit(),  # 止盈
        pm.monitor_sync_positions(),  # 防断网
        pm.schedule_daily_report(),  # 🔥 新增：每日日报
        start_monitor(pm)  # 监听
    )


if __name__ == "__main__":
    try:
        # 正确写法：run() 调用 main() 协程，main() 内部再 await gather
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程序由用户手动停止")
    except Exception as e:
        print(f"❌ 程序崩溃: {e}")
