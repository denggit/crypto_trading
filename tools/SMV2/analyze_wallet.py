#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 V2 (超严格版)
              - 更严格的评分标准，专门筛选超强钱包
              - 多维度评分：盈利力、持久力、真实性
              - 垃圾地址自动识别和过滤
              - 时间窗口分析（7天、30天）
@Author     : Auto-generated
@Date       : 2026-02-02
"""
import argparse
import asyncio
import logging
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp

# 导入配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import HELIUS_API_KEY, JUPITER_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000
JUPITER_QUOTE_TIMEOUT = 5  # 降低超时时间以提升速度
JUPITER_MAX_RETRIES = 1  # 减少重试次数以提升速度
MIN_COST_THRESHOLD = 0.05  # 最小成本阈值
DUST_THRESHOLD = 0.01  # 粉尘阈值：未实现收益低于此值的代币视为粉尘
WSOL_MINT = "So11111111111111111111111111111111111111112"

# === 🎯 V2 评分阈值配置 ===
# 垃圾地址识别阈值
FAST_GUN_THRESHOLD_MINUTES = 1  # 快枪手：平均持仓时间 < 1 分钟
ZERO_WARRIOR_WIN_RATE = 0.90  # 归零战神：胜率 >= 90%
ZERO_WARRIOR_MAX_LOSS = -0.95  # 归零战神：最大亏损 <= -95%
INSIDER_DOG_MAX_TOKENS = 2  # 内幕狗：交易过的代币数 <= 2

# S级战神标准
S_TIER_MIN_TOKENS_30D = 50  # 30天交易代币数
S_TIER_MIN_WIN_RATE = 0.65  # 胜率
S_TIER_MIN_PROFIT_30D = 200  # 30天总盈利 (SOL)
S_TIER_MIN_HOLD_TIME_HOURS = 2  # 平均持仓时间 (小时)
S_TIER_MAX_SINGLE_LOSS = -0.50  # 最大单笔亏损不能超过 -50%

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TransactionParser:
    """
    交易解析器：负责解析单笔交易中的 SOL 和代币变动
    
    职责：
    - 统计原生 SOL 变动
    - 统计 WSOL 变动
    - 统计其他代币变动
    - 合并 SOL/WSOL 避免重复计算
    """
    
    def __init__(self, target_wallet: str, wsol_mint: str = WSOL_MINT):
        """
        初始化交易解析器
        
        Args:
            target_wallet: 目标钱包地址
            wsol_mint: WSOL 代币地址
        """
        self.target_wallet = target_wallet
        self.wsol_mint = wsol_mint
    
    def parse_transaction(self, tx: dict) -> Tuple[float, Dict[str, float], int]:
        """
        解析单笔交易，返回 SOL 净变动和代币变动
        
        Args:
            tx: 交易数据字典
            
        Returns:
            (sol_change, token_changes, timestamp): SOL 净变动、代币变动字典、时间戳
        """
        timestamp = tx.get('timestamp', 0)
        native_sol_change = 0.0
        wsol_change = 0.0
        token_changes = defaultdict(float)
        
        # 1. 统计原生 SOL 变动
        for nt in tx.get('nativeTransfers', []):
            if nt.get('fromUserAccount') == self.target_wallet:
                native_sol_change -= nt.get('amount', 0) / 1e9
            if nt.get('toUserAccount') == self.target_wallet:
                native_sol_change += nt.get('amount', 0) / 1e9
        
        # 2. 统计 WSOL 和其他代币变动
        for tt in tx.get('tokenTransfers', []):
            mint = tt.get('mint', '')
            amt = tt.get('tokenAmount', 0)
            
            if mint == self.wsol_mint:
                if tt.get('fromUserAccount') == self.target_wallet:
                    wsol_change -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    wsol_change += amt
            else:
                if tt.get('fromUserAccount') == self.target_wallet:
                    token_changes[mint] -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    token_changes[mint] += amt
        
        # 3. 合并 SOL/WSOL，避免重复计算
        sol_change = self._merge_sol_changes(native_sol_change, wsol_change)
        
        return sol_change, dict(token_changes), timestamp
    
    def _merge_sol_changes(self, native_sol: float, wsol: float) -> float:
        """
        合并原生 SOL 和 WSOL 变动，避免重复计算
        
        Args:
            native_sol: 原生 SOL 变动
            wsol: WSOL 变动
            
        Returns:
            合并后的 SOL 净变动
        """
        if abs(native_sol) < 1e-9:
            return wsol
        if abs(wsol) < 1e-9:
            return native_sol
        
        # 同向变动：可能是包装/解包操作，取绝对值较大的
        if native_sol * wsol > 0:
            return native_sol if abs(native_sol) > abs(wsol) else wsol
        
        # 反向变动：正常交易，直接相加
        return native_sol + wsol


class TokenAttributionCalculator:
    """
    代币归因计算器：负责将 SOL 成本/收益按比例分配给多个代币
    """
    
    @staticmethod
    def calculate_attribution(
        sol_change: float,
        token_changes: Dict[str, float]
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        计算代币归因：按代币数量比例分配 SOL 成本/收益
        
        Args:
            sol_change: SOL 净变动（负数为支出，正数为收入）
            token_changes: 代币变动字典 {mint: amount}
            
        Returns:
            (buy_attributions, sell_attributions): 买入和卖出的 SOL 归因字典
        """
        buy_attributions = {}
        sell_attributions = {}
        
        if abs(sol_change) < 1e-9:
            return buy_attributions, sell_attributions
        
        # 分离买入和卖出
        buys = {mint: amt for mint, amt in token_changes.items() if amt > 0}
        sells = {mint: abs(amt) for mint, amt in token_changes.items() if amt < 0}
        
        if sol_change < 0:  # 支出 SOL -> 买入成本
            total_buy_tokens = sum(buys.values())
            if total_buy_tokens > 0:
                cost_per_token = abs(sol_change) / total_buy_tokens
                for mint, token_amount in buys.items():
                    buy_attributions[mint] = cost_per_token * token_amount
        
        elif sol_change > 0:  # 收入 SOL -> 卖出收益
            total_sell_tokens = sum(sells.values())
            if total_sell_tokens > 0:
                proceeds_per_token = sol_change / total_sell_tokens
                for mint, token_amount in sells.items():
                    sell_attributions[mint] = proceeds_per_token * token_amount
        
        return buy_attributions, sell_attributions


class PriceFetcher:
    """
    价格获取器：负责获取代币价格（直接获取 SOL 价格）
    """
    
    def __init__(self, session: aiohttp.ClientSession, jupiter_api_key: str = None):
        """
        初始化价格获取器
        
        Args:
            session: aiohttp 会话对象
            jupiter_api_key: Jupiter API 密钥（可选）
        """
        self.session = session
        self.jupiter_api_key = jupiter_api_key or JUPITER_API_KEY
        self._price_cache: Dict[str, float] = {}
    
    async def get_token_prices_in_sol(
        self,
        token_mints: List[str],
        max_retries: int = JUPITER_MAX_RETRIES
    ) -> Dict[str, float]:
        """
        批量获取代币对 SOL 的价格
        
        Args:
            token_mints: 代币地址列表
            max_retries: 最大重试次数
            
        Returns:
            价格字典 {mint: price_sol}
        """
        if not token_mints:
            return {}
        
        prices = {}
        mints_list = list(set(token_mints))  # 去重
        
        # 优化：先查询缓存中已有的，减少API调用
        cached_prices = {}
        uncached_mints = []
        for mint in mints_list:
            if mint in self._price_cache:
                cached_prices[mint] = self._price_cache[mint]
            else:
                uncached_mints.append(mint)
        
        # 只对未缓存的代币进行API查询（串行，因为API不能并发）
        for i, mint in enumerate(uncached_mints):
            try:
                result = await self._get_single_token_price_sol(mint, max_retries)
                if result is not None and result > 0:
                    prices[mint] = result
                    self._price_cache[mint] = result
                
                # API调用间隔：除了最后一个，每个调用后等待1秒
                if i < len(uncached_mints) - 1:
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.debug(f"获取 {mint[:8]}... 价格失败: {e}")
                # 即使失败也要等待，确保API调用间隔
                if i < len(uncached_mints) - 1:
                    await asyncio.sleep(1.0)
                continue
        
        # 合并缓存和查询结果
        prices.update(cached_prices)
        
        return prices
    
    async def _get_single_token_price_sol(
        self,
        token_mint: str,
        max_retries: int
    ) -> Optional[float]:
        """
        获取单个代币对 SOL 的价格
        
        Args:
            token_mint: 代币地址
            max_retries: 最大重试次数
            
        Returns:
            代币的 SOL 价格，失败返回 None
        """
        # 检查缓存
        if token_mint in self._price_cache:
            return self._price_cache[token_mint]
        
        # 如果是 WSOL，直接返回 1
        if token_mint == WSOL_MINT:
            return 1.0
        
        # 使用 Jupiter API 询价（优化：优先尝试最常见的decimals）
        test_amounts = [
            int(1e9),  # 1 个代币（9 位小数，最常见）
            int(1e6),  # 1 个代币（6 位小数）
            # 移除8位小数，减少API调用次数
        ]
        
        url = "https://api.jup.ag/swap/v1/quote"
        headers = {"Accept": "application/json"}
        if self.jupiter_api_key:
            headers["x-api-key"] = self.jupiter_api_key
        
        timeout = aiohttp.ClientTimeout(total=JUPITER_QUOTE_TIMEOUT)
        
        for quote_idx, quote_amount in enumerate(test_amounts):
            params = {
                "inputMint": token_mint,
                "outputMint": WSOL_MINT,
                "amount": str(quote_amount),
                "slippageBps": "50",
                "onlyDirectRoutes": "false",
            }
            
            # 不同quote_amount之间等待1秒
            if quote_idx > 0:
                await asyncio.sleep(1.0)
            
            for attempt in range(max_retries):
                try:
                    async with self.session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            out_amount = int(data.get('outAmount', 0))
                            if out_amount > 0:
                                decimals = 6 if quote_amount == int(1e6) else (9 if quote_amount == int(1e9) else 8)
                                price_sol = (out_amount / 1e9) / (quote_amount / (10 ** decimals))
                                if 0.000001 <= price_sol <= 1000:
                                    # 成功获取价格后，等待1秒（为下一个API调用做准备）
                                    await asyncio.sleep(1.0)
                                    return price_sol
                            # 即使out_amount为0，也要等待1秒
                            await asyncio.sleep(1.0)
                        elif resp.status == 429:
                            wait_time = max((attempt + 1) * 2, 1.0)  # 至少等待1秒
                            logger.debug(f"Jupiter rate limited, waiting {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # 非200状态码，等待1秒
                            await asyncio.sleep(1.0)
                            if attempt < max_retries - 1:
                                continue
                            else:
                                break
                except asyncio.TimeoutError:
                    # 超时后等待1秒
                    await asyncio.sleep(1.0)
                    if attempt < max_retries - 1:
                        continue
                    else:
                        break
                except Exception as e:
                    logger.debug(f"Jupiter API error for {token_mint[:8]}...: {e}")
                    # 异常后等待1秒
                    await asyncio.sleep(1.0)
                    if attempt < max_retries - 1:
                        continue
                    else:
                        break
                
                # 每次尝试之间等待1秒（除了最后一次）
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)
        
        return None


class WalletAnalyzerV2:
    """
    钱包分析器 V2：核心分析引擎（超严格版）
    
    职责：
    - 获取交易历史
    - 解析交易并计算代币项目收益
    - 时间窗口分析（7天、30天）
    - 生成详细分析报告
    """
    
    def __init__(self, helius_api_key: str = None):
        """
        初始化钱包分析器
        
        Args:
            helius_api_key: Helius API 密钥
        """
        self.helius_api_key = helius_api_key or HELIUS_API_KEY
        if not self.helius_api_key:
            raise ValueError("HELIUS_API_KEY 未配置")
    
    async def fetch_history_pagination(
        self,
        session: aiohttp.ClientSession,
        address: str,
        max_count: int = 3000
    ) -> List[dict]:
        """
        分页获取钱包交易历史
        
        Args:
            session: aiohttp 会话对象
            address: 钱包地址
            max_count: 最大获取数量
            
        Returns:
            交易列表
        """
        all_txs = []
        last_signature = None
        retry_count = 0
        max_retries = 5
        
        while len(all_txs) < max_count:
            url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
            params = {
                "api-key": self.helius_api_key,
                "type": "SWAP",
                "limit": 100
            }
            if last_signature:
                params["before"] = last_signature
            
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.warning(f"Rate limit exceeded, stopping at {len(all_txs)} transactions")
                            break
                        wait_time = retry_count * 2
                        logger.info(f"Rate limited, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if resp.status != 200:
                        logger.warning(f"API returned status {resp.status}, stopping")
                        break
                    
                    data = await resp.json()
                    if not data:
                        break
                    
                    all_txs.extend(data)
                    if len(data) < 100:
                        break
                    
                    last_signature = data[-1].get('signature')
                    retry_count = 0
                    await asyncio.sleep(1.0)  # API调用间隔至少1秒
                    
            except aiohttp.ClientError as e:
                logger.error(f"Network error fetching transactions: {e}")
                break
            except Exception as e:
                logger.error(f"Unexpected error fetching transactions: {e}")
                break
        
        return all_txs[:max_count]
    
    async def parse_token_projects(
        self,
        session: aiohttp.ClientSession,
        transactions: List[dict],
        target_wallet: str
    ) -> Dict:
        """
        解析交易并计算每个代币项目的收益（V2版本：包含时间窗口分析）
        
        Args:
            session: aiohttp 会话对象
            transactions: 交易列表
            target_wallet: 目标钱包地址
            
        Returns:
            分析结果字典，包含详细指标
        """
        # 初始化组件
        parser = TransactionParser(target_wallet)
        attribution_calc = TokenAttributionCalculator()
        price_fetcher = PriceFetcher(session)
        
        # 项目数据：{mint: {buy_sol, sell_sol, buy_tokens, sell_tokens, first_time, last_time, transactions}}
        projects = defaultdict(lambda: {
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "buy_tokens": 0.0,
            "sell_tokens": 0.0,
            "first_time": 0,
            "last_time": 0,
            "transactions": []  # 记录每笔交易的详细信息
        })
        
        # 按时间倒序处理交易（从最早到最新）
        for tx in reversed(transactions):
            try:
                # 解析交易
                sol_change, token_changes, timestamp = parser.parse_transaction(tx)
                
                # 计算归因
                buy_attributions, sell_attributions = attribution_calc.calculate_attribution(
                    sol_change, token_changes
                )
                
                # 更新项目数据
                for mint, delta in token_changes.items():
                    # 更新代币数量
                    if delta > 0:
                        projects[mint]["buy_tokens"] += delta
                    else:
                        projects[mint]["sell_tokens"] += abs(delta)
                    
                    # 更新 SOL 成本/收益
                    if mint in buy_attributions:
                        projects[mint]["buy_sol"] += buy_attributions[mint]
                    if mint in sell_attributions:
                        projects[mint]["sell_sol"] += sell_attributions[mint]
                    
                    # 更新时间戳
                    if projects[mint]["first_time"] == 0 and timestamp > 0:
                        projects[mint]["first_time"] = timestamp
                    if timestamp > 0:
                        projects[mint]["last_time"] = timestamp
                    
                    # 记录交易详情
                    projects[mint]["transactions"].append({
                        "timestamp": timestamp,
                        "sol_change": sol_change,
                        "token_delta": delta,
                        "buy_sol": buy_attributions.get(mint, 0),
                        "sell_sol": sell_attributions.get(mint, 0)
                    })
                
                # 处理无 SOL 交易的跨代币兑换
                if abs(sol_change) < 1e-9 and token_changes:
                    for mint, delta in token_changes.items():
                        if delta > 0:
                            projects[mint]["buy_tokens"] += delta
                        else:
                            projects[mint]["sell_tokens"] += abs(delta)
                            
            except Exception as e:
                logger.warning(f"Error parsing transaction: {e}")
                continue
        
        # 获取当前价格并计算最终收益
        active_mints = [
            m for m, v in projects.items()
            if (v["buy_tokens"] - v["sell_tokens"]) > 0 and v["buy_sol"] >= MIN_COST_THRESHOLD
        ]
        
        # 优化：如果持仓代币太多，只查询前50个（避免查询时间过长）
        if len(active_mints) > 50:
            logger.debug(f"持仓代币过多({len(active_mints)}个)，仅查询前50个的价格")
            active_mints = active_mints[:50]
        
        if active_mints:
            logger.debug(f"正在获取 {len(active_mints)} 个代币的 SOL 价格...")
            prices_sol = await price_fetcher.get_token_prices_in_sol(active_mints)
        else:
            prices_sol = {}
        
        # 生成最终结果
        final_results = []
        for mint, data in projects.items():
            if data["buy_sol"] < MIN_COST_THRESHOLD:
                continue
            
            remaining_tokens = max(0, data["buy_tokens"] - data["sell_tokens"])
            price_sol = prices_sol.get(mint, 0)
            
            # 计算收益
            if price_sol == 0 and remaining_tokens > 0:
                unrealized_sol = 0
            else:
                unrealized_sol = remaining_tokens * price_sol
            
            total_value_sol = data["sell_sol"] + unrealized_sol
            net_profit = total_value_sol - data["buy_sol"]
            roi = (total_value_sol / data["buy_sol"] - 1) if data["buy_sol"] > 0 else 0
            
            # 计算持仓时间
            hold_time_minutes = 0
            if data["last_time"] > 0 and data["first_time"] > 0:
                hold_time_minutes = (data["last_time"] - data["first_time"]) / 60
            
            # 计算未结算部分的成本（按比例分配）
            unsettled_cost = 0.0
            if remaining_tokens > 0 and data["buy_tokens"] > 0:
                unsettled_cost = data["buy_sol"] * (remaining_tokens / data["buy_tokens"])
            
            final_results.append({
                "token": mint,
                "cost": data["buy_sol"],
                "profit": net_profit,
                "roi": roi,
                "is_win": net_profit > 0,
                "hold_time": hold_time_minutes,
                "first_time": data["first_time"],
                "last_time": data["last_time"],
                "transactions": data["transactions"],
                "has_price": price_sol > 0,
                "remaining_tokens": remaining_tokens,  # 剩余代币数量
                "unrealized_sol": unrealized_sol,  # 未实现收益（SOL）
                "unsettled_cost": unsettled_cost,  # 未结算部分的成本
                "is_unsettled": remaining_tokens > 0  # 是否未结算
            })
        
        return {
            "results": final_results,
            "prices": prices_sol
        }


class WalletScorerV2:
    """
    钱包评分器 V2：超严格评分系统
    
    职责：
    - 计算多维度评分（盈利力、持久力、真实性）
    - 识别垃圾地址
    - 生成最终评分和定位
    """
    
    @staticmethod
    def calculate_scores(analysis_result: Dict, current_time: int = None) -> Dict:
        """
        计算钱包详细评分
        
        Args:
            analysis_result: 分析结果字典（包含 results 和 prices）
            current_time: 当前时间戳（秒），如果为 None 则使用当前时间
            
        Returns:
            评分结果字典
        """
        results = analysis_result.get("results", [])
        
        if not results:
            return {
                "final_score": 0,
                "tier": "F",
                "description": "无数据",
                "dimensions": {},
                "flags": {"is_trash": True, "reasons": ["无交易数据"]},
                "positioning": {}
            }
        
        if current_time is None:
            current_time = int(datetime.now().timestamp())
        
        # 计算时间窗口（7天、30天）
        time_7d = current_time - 7 * 24 * 3600
        time_30d = current_time - 30 * 24 * 3600
        
        # 分离盈利和亏损项目
        wins = [r for r in results if r.get('is_win', False)]
        losses = [r for r in results if not r.get('is_win', False)]
        
        # === 1. 盈利力维度 ===
        profit_dimension = WalletScorerV2._calculate_profit_dimension(
            results, wins, losses, time_7d, time_30d
        )
        
        # === 2. 持久力维度 ===
        persistence_dimension = WalletScorerV2._calculate_persistence_dimension(
            results, time_7d, time_30d
        )
        
        # === 3. 真实性维度 ===
        authenticity_dimension = WalletScorerV2._calculate_authenticity_dimension(
            results, wins, losses
        )
        
        # === 4. 垃圾地址识别 ===
        flags = WalletScorerV2._identify_trash_addresses(
            results, wins, losses, profit_dimension, persistence_dimension, authenticity_dimension
        )
        
        # === 5. 计算定位 ===
        positioning = WalletScorerV2._calculate_positioning(
            profit_dimension, persistence_dimension, authenticity_dimension
        )
        
        # === 6. 计算最终评分 ===
        final_score, tier, description = WalletScorerV2._calculate_final_score(
            profit_dimension, persistence_dimension, authenticity_dimension, flags
        )
        
        return {
            "final_score": final_score,
            "tier": tier,
            "description": description,
            "dimensions": {
                "profit": profit_dimension,
                "persistence": persistence_dimension,
                "authenticity": authenticity_dimension
            },
            "flags": flags,
            "positioning": positioning
        }
    
    @staticmethod
    def _calculate_profit_dimension(
        results: List[dict],
        wins: List[dict],
        losses: List[dict],
        time_7d: int,
        time_30d: int
    ) -> Dict:
        """
        计算盈利力维度
        
        Returns:
            盈利力维度评分和指标
        """
        # 基础指标
        total_profit = sum(r.get('profit', 0) for r in results)
        win_profit = sum(r.get('profit', 0) for r in wins)
        loss_profit = abs(sum(r.get('profit', 0) for r in losses))
        profit_factor = win_profit / loss_profit if loss_profit > 0 else (win_profit if win_profit > 0 else 0)
        
        # 计算排除最高收益代币后的盈利（更能反映持续盈利能力）
        if results:
            # 找到收益最高的代币
            max_profit_result = max(results, key=lambda x: x.get('profit', 0))
            max_profit = max_profit_result.get('profit', 0)
            max_profit_cost = max_profit_result.get('cost', 0)
            
            # 排除最高收益代币后的总盈利和总成本
            profit_excluding_max = total_profit - max_profit
            total_cost = sum(r.get('cost', 0) for r in results)
            cost_excluding_max = total_cost - max_profit_cost
            
            # 排除最高收益后的盈利百分比
            profit_pct_excluding_max = (profit_excluding_max / cost_excluding_max * 100) if cost_excluding_max > 0 else 0
        else:
            profit_pct_excluding_max = 0
            max_profit = 0
        
        # 时间窗口分析
        results_7d = [r for r in results if r.get('last_time', 0) >= time_7d]
        results_30d = [r for r in results if r.get('last_time', 0) >= time_30d]
        
        profit_7d = sum(r.get('profit', 0) for r in results_7d)
        profit_30d = sum(r.get('profit', 0) for r in results_30d)
        
        # 计算百分比（相对于总成本）
        total_cost = sum(r.get('cost', 0) for r in results)
        cost_7d = sum(r.get('cost', 0) for r in results_7d)
        cost_30d = sum(r.get('cost', 0) for r in results_30d)
        
        profit_pct_7d = (profit_7d / cost_7d * 100) if cost_7d > 0 else 0
        profit_pct_30d = (profit_30d / cost_30d * 100) if cost_30d > 0 else 0
        
        # 单币ROI统计
        rois = [r.get('roi', 0) for r in results]
        max_roi = max(rois) if rois else 0
        avg_roi = statistics.mean(rois) if rois else 0
        median_roi = statistics.median(rois) if rois else 0
        
        # 最大单笔亏损
        max_single_loss = min([r.get('roi', 0) for r in losses]) if losses else 0
        
        # 盈利力评分（0-100）
        profit_score = 0
        
        # 盈亏比评分（30分）
        if profit_factor >= 5:
            profit_score += 30
        elif profit_factor >= 3:
            profit_score += 25
        elif profit_factor >= 2:
            profit_score += 20
        elif profit_factor >= 1.5:
            profit_score += 15
        elif profit_factor >= 1:
            profit_score += 10
        elif profit_factor > 0:
            profit_score += 5
        
        # 30天盈利评分（30分）- 按百分比计算
        if profit_pct_30d >= 100:  # >= 100%
            profit_score += 30
        elif profit_pct_30d >= 80:
            profit_score += 25
        elif profit_pct_30d >= 50:
            profit_score += 20
        elif profit_pct_30d >= 30:
            profit_score += 15
        elif profit_pct_30d >= 10:
            profit_score += 10
        elif profit_pct_30d > 0:
            profit_score += 5
        
        # 7天盈利评分（20分）- 按百分比计算
        if profit_pct_7d >= 30:  # >= 30%
            profit_score += 20
        elif profit_pct_7d >= 20:
            profit_score += 15
        elif profit_pct_7d >= 10:
            profit_score += 10
        elif profit_pct_7d > 0:
            profit_score += 5
        
        # 单币ROI评分（20分）
        if max_roi >= 10:  # 10倍以上
            profit_score += 20
        elif max_roi >= 5:
            profit_score += 15
        elif max_roi >= 2:
            profit_score += 10
        elif max_roi >= 1:
            profit_score += 5
        
        return {
            "score": min(100, profit_score),
            "total_profit": total_profit,
            "profit_factor": profit_factor,
            "profit_7d": profit_7d,
            "profit_pct_7d": profit_pct_7d,
            "profit_30d": profit_30d,
            "profit_pct_30d": profit_pct_30d,
            "profit_pct_excluding_max": profit_pct_excluding_max,  # 排除最高收益后的盈利百分比
            "max_roi": max_roi,
            "avg_roi": avg_roi,
            "median_roi": median_roi,
            "max_single_loss": max_single_loss
        }
    
    @staticmethod
    def _calculate_persistence_dimension(
        results: List[dict],
        time_7d: int,
        time_30d: int
    ) -> Dict:
        """
        计算持久力维度
        
        Returns:
            持久力维度评分和指标
        """
        # 基础胜率
        wins = [r for r in results if r.get('is_win', False)]
        win_rate = len(wins) / len(results) if results else 0
        
        # 时间窗口分析
        results_7d = [r for r in results if r.get('last_time', 0) >= time_7d]
        results_30d = [r for r in results if r.get('last_time', 0) >= time_30d]
        
        # 交易频次
        tokens_7d = len(set(r.get('token', '') for r in results_7d))
        tokens_30d = len(set(r.get('token', '') for r in results_30d))
        tx_count_7d = len(results_7d)
        tx_count_30d = len(results_30d)
        
        # 持久力评分（0-100）
        persistence_score = 0
        
        # 胜率评分（40分）
        if win_rate >= 0.70:
            persistence_score += 40
        elif win_rate >= 0.65:
            persistence_score += 35
        elif win_rate >= 0.60:
            persistence_score += 30
        elif win_rate >= 0.55:
            persistence_score += 25
        elif win_rate >= 0.50:
            persistence_score += 20
        elif win_rate >= 0.45:
            persistence_score += 15
        elif win_rate >= 0.40:
            persistence_score += 10
        elif win_rate > 0:
            persistence_score += 5
        
        # 30天交易频次评分（30分）
        if tokens_30d >= 50:
            persistence_score += 30
        elif tokens_30d >= 30:
            persistence_score += 25
        elif tokens_30d >= 20:
            persistence_score += 20
        elif tokens_30d >= 10:
            persistence_score += 15
        elif tokens_30d >= 5:
            persistence_score += 10
        elif tokens_30d > 0:
            persistence_score += 5
        
        # 7天交易频次评分（30分）
        if tokens_7d >= 20:
            persistence_score += 30
        elif tokens_7d >= 15:
            persistence_score += 25
        elif tokens_7d >= 10:
            persistence_score += 20
        elif tokens_7d >= 5:
            persistence_score += 15
        elif tokens_7d >= 3:
            persistence_score += 10
        elif tokens_7d > 0:
            persistence_score += 5
        
        return {
            "score": min(100, persistence_score),
            "win_rate": win_rate,
            "tokens_7d": tokens_7d,
            "tx_count_7d": tx_count_7d,
            "tokens_30d": tokens_30d,
            "tx_count_30d": tx_count_30d
        }
    
    @staticmethod
    def _calculate_authenticity_dimension(
        results: List[dict],
        wins: List[dict],
        losses: List[dict]
    ) -> Dict:
        """
        计算真实性维度
        
        Returns:
            真实性维度评分和指标
        """
        # 平均持仓时间
        hold_times = [r.get('hold_time', 0) for r in results if r.get('hold_time', 0) > 0]
        avg_hold_time = statistics.mean(hold_times) if hold_times else 0
        median_hold_time = statistics.median(hold_times) if hold_times else 0
        
        # 盈利代币平均持仓时间
        win_hold_times = [r.get('hold_time', 0) for r in wins if r.get('hold_time', 0) > 0]
        avg_win_hold_time = statistics.mean(win_hold_times) if win_hold_times else 0
        
        # 亏损代币平均持仓时间
        loss_hold_times = [r.get('hold_time', 0) for r in losses if r.get('hold_time', 0) > 0]
        avg_loss_hold_time = statistics.mean(loss_hold_times) if loss_hold_times else 0
        
        # 代币多样性
        unique_tokens = len(set(r.get('token', '') for r in results))
        
        # 真实性评分（0-100）
        authenticity_score = 0
        
        # 平均持仓时间评分（40分）- 不能太快也不能太慢
        if 60 <= avg_hold_time <= 480:  # 1小时到8小时
            authenticity_score += 40
        elif 30 <= avg_hold_time <= 720:  # 30分钟到12小时
            authenticity_score += 35
        elif 15 <= avg_hold_time <= 1440:  # 15分钟到24小时
            authenticity_score += 30
        elif 5 <= avg_hold_time <= 2880:  # 5分钟到48小时
            authenticity_score += 25
        elif avg_hold_time > 0:
            authenticity_score += 10
        
        # 代币多样性评分（40分）
        if unique_tokens >= 50:
            authenticity_score += 40
        elif unique_tokens >= 30:
            authenticity_score += 35
        elif unique_tokens >= 20:
            authenticity_score += 30
        elif unique_tokens >= 10:
            authenticity_score += 25
        elif unique_tokens >= 5:
            authenticity_score += 20
        elif unique_tokens >= 3:
            authenticity_score += 15
        elif unique_tokens > 1:
            authenticity_score += 10
        
        # 盈利/亏损持仓时间差异评分（20分）
        # 如果盈利代币持仓时间明显长于亏损代币，说明有纪律
        if avg_win_hold_time > 0 and avg_loss_hold_time > 0:
            hold_time_ratio = avg_win_hold_time / avg_loss_hold_time
            if 1.2 <= hold_time_ratio <= 3.0:  # 盈利持仓时间略长，说明有策略
                authenticity_score += 20
            elif 0.8 <= hold_time_ratio <= 1.2:  # 接近，说明一致性
                authenticity_score += 15
            elif hold_time_ratio > 3.0:  # 差异太大，可能有问题
                authenticity_score += 10
            else:
                authenticity_score += 5
        
        return {
            "score": min(100, authenticity_score),
            "avg_hold_time": avg_hold_time,
            "median_hold_time": median_hold_time,
            "avg_win_hold_time": avg_win_hold_time,
            "avg_loss_hold_time": avg_loss_hold_time,
            "unique_tokens": unique_tokens
        }
    
    @staticmethod
    def _identify_trash_addresses(
        results: List[dict],
        wins: List[dict],
        losses: List[dict],
        profit_dim: Dict,
        persistence_dim: Dict,
        authenticity_dim: Dict
    ) -> Dict:
        """
        识别垃圾地址
        
        Returns:
            垃圾地址标识和原因
        """
        flags = {
            "is_trash": False,
            "reasons": []
        }
        
        # 基础指标
        win_rate = persistence_dim.get("win_rate", 0)
        max_loss = profit_dim.get("max_single_loss", 0)
        unique_tokens = authenticity_dim.get("unique_tokens", 0)
        total_profit = profit_dim.get("total_profit", 0)
        profit_factor = profit_dim.get("profit_factor", 0)
        avg_hold_time = authenticity_dim.get("avg_hold_time", 0)
        
        # 1. 快枪手：平均持仓时间 < 1 分钟
        if avg_hold_time < FAST_GUN_THRESHOLD_MINUTES:
            flags["is_trash"] = True
            flags["reasons"].append("快枪手：平均持仓时间 < 1 分钟")
        
        # 2. 归零战神：胜率 >= 90% 且最大亏损 <= -95%
        if win_rate >= ZERO_WARRIOR_WIN_RATE and max_loss <= ZERO_WARRIOR_MAX_LOSS:
            flags["is_trash"] = True
            flags["reasons"].append("归零战神：胜率高但一输就归零")
        
        # 3. 内幕狗：只交易过 1-2 个代币
        if unique_tokens <= INSIDER_DOG_MAX_TOKENS:
            flags["is_trash"] = True
            flags["reasons"].append(f"内幕狗：只交易过 {unique_tokens} 个代币")
        
        # 4. 交易超过5个代币但目前仍然处于亏损
        if unique_tokens > 5 and total_profit < 0:
            flags["is_trash"] = True
            flags["reasons"].append(f"交易{unique_tokens}个代币但仍亏损 {total_profit:.2f} SOL")
        
        # 5. 超过两个代币交易亏损<=-95%
        if unique_tokens > 2:
            # 统计亏损<=-95%的代币数量
            severe_losses = [r for r in losses if r.get('roi', 0) <= -0.95]
            if len(severe_losses) >= 2:
                flags["is_trash"] = True
                flags["reasons"].append(f"有{len(severe_losses)}个代币亏损<=-95%")
        
        # 6. 交易超过5个代币，盈亏比小于1
        if unique_tokens > 5 and profit_factor < 1.0:
            flags["is_trash"] = True
            flags["reasons"].append(f"交易{unique_tokens}个代币但盈亏比{profit_factor:.2f} < 1")
        
        # 7. 胜率小于40%的同时盈亏比小于2
        if win_rate < 0.40 and profit_factor < 2.0:
            flags["is_trash"] = True
            flags["reasons"].append(f"胜率{win_rate:.1%} < 40% 且盈亏比{profit_factor:.2f} < 2")
        
        # 8. 最大单笔亏损超过 -50%（不符合S级标准，仅警告）
        if max_loss < S_TIER_MAX_SINGLE_LOSS:
            flags["reasons"].append(f"最大单笔亏损 {max_loss:.1%} 超过 -50%，缺乏止损纪律")
        
        return flags
    
    @staticmethod
    def _calculate_positioning(
        profit_dim: Dict,
        persistence_dim: Dict,
        authenticity_dim: Dict
    ) -> Dict:
        """
        计算钱包定位
        
        Returns:
            定位评分字典
        """
        positioning = {}
        
        # 🛡️ 稳健中军：胜率高、盈亏比好、持仓时间适中
        stability_score = (
            persistence_dim.get("score", 0) * 0.4 +
            profit_dim.get("score", 0) * 0.4 +
            authenticity_dim.get("score", 0) * 0.2
        )
        positioning["🛡️ 稳健中军"] = int(stability_score)
        
        # ⚔️ 土狗猎手：盈亏比极高、单币ROI高、交易频次高
        hunter_score = (
            profit_dim.get("score", 0) * 0.5 +
            persistence_dim.get("score", 0) * 0.3 +
            authenticity_dim.get("score", 0) * 0.2
        )
        positioning["⚔️ 土狗猎手"] = int(hunter_score)
        
        # 💎 钻石之手：持仓时间长、胜率高、代币多样性好
        diamond_score = (
            authenticity_dim.get("score", 0) * 0.5 +
            persistence_dim.get("score", 0) * 0.3 +
            profit_dim.get("score", 0) * 0.2
        )
        positioning["💎 钻石之手"] = int(diamond_score)
        
        # 🚀 短线高手：交易频次高、胜率高、持仓时间短但有效
        if authenticity_dim.get("avg_hold_time", 0) < 120:  # 2小时以内
            short_term_score = (
                persistence_dim.get("score", 0) * 0.5 +
                profit_dim.get("score", 0) * 0.3 +
                authenticity_dim.get("score", 0) * 0.2
            )
            positioning["🚀 短线高手"] = int(short_term_score)
        else:
            positioning["🚀 短线高手"] = 0
        
        return positioning
    
    @staticmethod
    def _calculate_final_score(
        profit_dim: Dict,
        persistence_dim: Dict,
        authenticity_dim: Dict,
        flags: Dict
    ) -> Tuple[int, str, str]:
        """
        计算最终评分
        
        Returns:
            (final_score, tier, description)
        """
        # 如果被标记为垃圾地址，直接给低分
        if flags.get("is_trash", False):
            return 0, "F", "垃圾地址：" + " | ".join(flags.get("reasons", []))
        
        # 加权平均
        final_score = (
            profit_dim.get("score", 0) * 0.45 +  # 盈利力权重最高
            persistence_dim.get("score", 0) * 0.35 +  # 持久力次之
            authenticity_dim.get("score", 0) * 0.20  # 真实性
        )
        
        # 根据S级标准进行额外加分
        profit_pct_30d = profit_dim.get("profit_pct_30d", 0)
        tokens_30d = persistence_dim.get("tokens_30d", 0)
        win_rate = persistence_dim.get("win_rate", 0)
        avg_hold_hours = authenticity_dim.get("avg_hold_time", 0) / 60
        max_loss = profit_dim.get("max_single_loss", 0)
        
        # S级加分（最多+20分）
        bonus = 0
        if profit_pct_30d >= 100:  # 30天盈利 >= 100%
            bonus += 5
        if tokens_30d >= S_TIER_MIN_TOKENS_30D:
            bonus += 5
        if win_rate >= S_TIER_MIN_WIN_RATE:
            bonus += 5
        if avg_hold_hours >= S_TIER_MIN_HOLD_TIME_HOURS:
            bonus += 3
        if max_loss >= S_TIER_MAX_SINGLE_LOSS:  # 没有超过-50%
            bonus += 2
        
        final_score = min(100, int(final_score + bonus))
        
        # 评级
        if final_score >= 90:
            tier = "S"
        elif final_score >= 80:
            tier = "A"
        elif final_score >= 70:
            tier = "B"
        elif final_score >= 60:
            tier = "C"
        else:
            tier = "F"
        
        # 描述
        description = (
            f"盈利力:{profit_dim.get('score', 0)} | "
            f"持久力:{persistence_dim.get('score', 0)} | "
            f"真实性:{authenticity_dim.get('score', 0)}"
        )
        
        return final_score, tier, description


async def main():
    """主函数：命令行入口"""
    parser = argparse.ArgumentParser(description="智能钱包画像识别工具 V2 (超严格版)")
    parser.add_argument("wallet", help="钱包地址")
    parser.add_argument("--max-txs", type=int, default=TARGET_TX_COUNT, help="最大交易数量")
    args = parser.parse_args()
    
    analyzer = WalletAnalyzerV2()
    
    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V2 (超严格版): {args.wallet[:6]}...")
        txs = await analyzer.fetch_history_pagination(session, args.wallet, args.max_txs)
        
        if not txs:
            print("❌ 未获取到交易数据")
            return
        
        print(f"📊 获取到 {len(txs)} 笔交易，开始分析...")
        analysis_result = await analyzer.parse_token_projects(session, txs, args.wallet)
        
        if not analysis_result.get("results"):
            print("❌ 未找到有效的代币项目")
            return
        
        # 计算评分
        scores = WalletScorerV2.calculate_scores(analysis_result)
        
        print("\n" + "═" * 70)
        print(f"🧬 战力报告 V2 (超严格版): {args.wallet[:6]}...")
        print("═" * 70)
        
        results = analysis_result["results"]
        dims = scores["dimensions"]
        profit_dim = dims["profit"]
        persistence_dim = dims["persistence"]
        authenticity_dim = dims["authenticity"]
        
        print(f"📊 核心汇总:")
        print(f"   • 项目总数: {len(results)}")
        print(f"   • 胜率: {persistence_dim['win_rate']:.1%}")
        print(f"   • 盈亏比: {profit_dim['profit_factor']:.2f}")
        print(f"   • 累计利润: {profit_dim['total_profit']:+,.2f} SOL")
        print(f"   • 30天利润: {profit_dim['profit_30d']:+,.2f} SOL ({profit_dim['profit_pct_30d']:.1f}%)")
        print(f"   • 7天利润: {profit_dim['profit_7d']:+,.2f} SOL ({profit_dim['profit_pct_7d']:.1f}%)")
        print(f"   • 排除最高收益后盈利: {profit_dim.get('profit_pct_excluding_max', 0):.1f}%")
        print(f"   • 平均持仓: {authenticity_dim['avg_hold_time']:.1f} 分钟")
        print(f"   • 代币多样性: {authenticity_dim['unique_tokens']} 个")
        print(f"   • 30天交易: {persistence_dim['tokens_30d']} 个代币, {persistence_dim['tx_count_30d']} 笔")
        
        print("-" * 70)
        print(f"🎯 维度评分:")
        print(f"   • 盈利力: {profit_dim['score']}/100")
        print(f"   • 持久力: {persistence_dim['score']}/100")
        print(f"   • 真实性: {authenticity_dim['score']}/100")
        
        print("-" * 70)
        print(f"📍 定位评分:")
        for role, score in scores["positioning"].items():
            bar_length = score // 10
            bar = '█' * bar_length + '░' * (10 - bar_length)
            print(f"   {role}: {bar} {score}分")
        
        print("-" * 70)
        print(f"🏆 综合评级: [{scores['tier']}级] {scores['final_score']} 分")
        print(f"📝 状态评价: {scores['description']}")
        
        if scores["flags"]["is_trash"]:
            print(f"⚠️  垃圾地址标识: {' | '.join(scores['flags']['reasons'])}")
        elif scores["flags"]["reasons"]:
            print(f"⚠️  警告: {' | '.join(scores['flags']['reasons'])}")
        
        print("-" * 70)
        
        print("\n📝 重点项目明细 (按利润排序):")
        results_sorted = sorted(results, key=lambda x: x['profit'], reverse=True)
        for r in results_sorted[:10]:
            status_icon = '🟢' if r['is_win'] else '🔴'
            token_short = r['token'][:8] + '..'
            profit = r['profit']
            roi_pct = r['roi'] * 100
            hold_time = r['hold_time']
            print(f" {status_icon} {token_short} | 利润 {profit:>+8.2f} SOL | ROI {roi_pct:>+7.1f}% | 持仓 {hold_time:>6.1f} 分钟")


if __name__ == "__main__":
    asyncio.run(main())
