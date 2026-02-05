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
import json
import logging
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import duckdb

# 导入配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import HELIUS_API_KEY, JUPITER_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 2000
JUPITER_QUOTE_TIMEOUT = 3  # 降低超时时间以提升速度（从5秒降到3秒）
JUPITER_MAX_RETRIES = 1  # 减少重试次数以提升速度
MIN_COST_THRESHOLD = 0.05  # 最小成本阈值
DUST_THRESHOLD = 0.01  # 粉尘阈值：未实现收益低于此值的代币视为粉尘
WSOL_MINT = "So11111111111111111111111111111111111111112"

# 数据库配置
DB_DIR = Path(__file__).parent / "data"
DB_FILE = DB_DIR / "transactions.duckdb"

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


class TransactionDBManager:
    """
    交易记录数据库管理器：使用DuckDB存储和查询交易记录
    
    职责：
    - 初始化数据库和表结构
    - 查询指定地址的交易记录
    - 保存交易记录到数据库
    - 管理数据库连接和事务
    """
    
    def __init__(self, db_file: Path = DB_FILE):
        """
        初始化数据库管理器
        
        Args:
            db_file: 数据库文件路径
        """
        self.db_file = Path(db_file).resolve()  # 转换为绝对路径
        # 确保数据库目录存在
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"数据库文件路径: {self.db_file}")
        self._init_database()
    
    def _init_database(self):
        """
        初始化数据库和表结构
        """
        try:
            db_path_str = str(self.db_file)
            logger.debug(f"正在连接数据库: {db_path_str}")
            conn = duckdb.connect(db_path_str)
            # 创建表：address (TEXT), signature (TEXT PRIMARY KEY), transaction_data (JSON)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    address TEXT NOT NULL,
                    signature TEXT NOT NULL PRIMARY KEY,
                    transaction_data JSON NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 创建索引以加速查询
            conn.execute("CREATE INDEX IF NOT EXISTS idx_address ON transactions(address)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signature ON transactions(signature)")
            conn.close()
            # 验证文件是否真的被创建
            if self.db_file.exists():
                file_size = self.db_file.stat().st_size
                logger.debug(f"数据库初始化完成: {self.db_file} (文件大小: {file_size} 字节)")
            else:
                logger.warning(f"数据库文件未创建: {self.db_file}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}", exc_info=True)
            raise
    
    def get_transactions(self, address: str, limit: Optional[int] = None) -> List[dict]:
        """
        获取指定地址的交易记录（按时间倒序，最新的在前）
        
        Args:
            address: 钱包地址
            limit: 最大返回数量，None表示返回所有
            
        Returns:
            交易记录列表（按时间倒序）
        """
        conn = None
        try:
            conn = duckdb.connect(str(self.db_file))
            query = """
                SELECT transaction_data
                FROM transactions
                WHERE address = ?
                ORDER BY created_at DESC
            """
            if limit:
                query += f" LIMIT {limit}"
            
            result = conn.execute(query, [address]).fetchall()
            
            # 解析JSON数据
            transactions = []
            for row in result:
                try:
                    tx_data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    transactions.append(tx_data)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"解析交易数据失败: {e}")
                    continue
            
            logger.debug(f"从数据库读取到 {len(transactions)} 条交易记录: {address[:8]}...")
            return transactions
        except Exception as e:
            logger.error(f"查询交易记录失败: {e}")
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"关闭数据库连接失败: {e}")
    
    def save_transactions(self, address: str, transactions: List[dict]):
        """
        保存交易记录到数据库（去重，支持并发安全）
        
        Args:
            address: 钱包地址
            transactions: 交易记录列表
        """
        if not transactions:
            return
        
        conn = None
        try:
            conn = duckdb.connect(str(self.db_file))
            
            # 获取已有的signature集合（用于本地去重，减少不必要的插入尝试）
            existing_sigs = set()
            result = conn.execute(
                "SELECT signature FROM transactions WHERE address = ?",
                [address]
            ).fetchall()
            existing_sigs = {row[0] for row in result}
            
            # 插入新交易（使用 INSERT OR IGNORE 处理并发插入时的重复键冲突）
            new_count = 0
            for tx in transactions:
                signature = tx.get('signature')
                if not signature or signature in existing_sigs:
                    continue
                
                try:
                    tx_json = json.dumps(tx, ensure_ascii=False) if not isinstance(tx, str) else tx
                    # 使用 INSERT OR IGNORE 避免并发插入时的重复键冲突
                    # 如果记录已存在，则忽略插入（不报错）
                    conn.execute(
                        "INSERT OR IGNORE INTO transactions (address, signature, transaction_data) VALUES (?, ?, ?)",
                        [address, signature, tx_json]
                    )
                    existing_sigs.add(signature)
                    new_count += 1
                except Exception as e:
                    # 如果 INSERT OR IGNORE 仍然失败（可能是其他错误），记录日志但不中断流程
                    # 注意：在并发场景下，即使使用 INSERT OR IGNORE，也可能因为其他原因失败
                    # 但这种情况应该很少见
                    logger.debug(f"插入交易记录失败 {signature[:8]}...: {e}")
                    continue
            
            conn.commit()
            
            if new_count > 0:
                logger.debug(f"已保存 {new_count} 条新交易记录到数据库: {address[:8]}...")
        except Exception as e:
            logger.error(f"保存交易记录到数据库失败: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"关闭数据库连接失败: {e}")
    
    def get_transaction_count(self, address: str) -> int:
        """
        获取指定地址的交易记录数量
        
        Args:
            address: 钱包地址
            
        Returns:
            交易记录数量
        """
        conn = None
        try:
            conn = duckdb.connect(str(self.db_file))
            result = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE address = ?",
                [address]
            ).fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"查询交易记录数量失败: {e}")
            return 0
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"关闭数据库连接失败: {e}")


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
        参考 monitor.py 的 parse_tx 逻辑处理 WSOL
        
        Args:
            tx: 交易数据字典
            
        Returns:
            (sol_change, token_changes, timestamp): SOL 净变动、代币变动字典、时间戳
        """
        # 获取时间戳，Helius API 返回的是 Unix 时间戳（秒）
        # 注意：Helius API 可能返回秒或毫秒格式，需要检查
        # 如果值很大（>1e10），可能是毫秒格式，需要转换
        timestamp_raw = tx.get('timestamp', 0)
        
        # 检查时间戳格式
        # Unix 时间戳（秒）通常在 1e9 到 1e10 之间（2001-2286年）
        # 如果 > 1e10，很可能是毫秒格式
        if timestamp_raw > 1e10:  # 可能是毫秒格式
            timestamp = int(timestamp_raw / 1000)
        else:
            timestamp = int(timestamp_raw)
        
        token_transfers = tx.get('tokenTransfers', [])
        native_transfers = tx.get('nativeTransfers', [])
        
        native_sol_change = 0.0
        wsol_change = 0.0
        token_changes = defaultdict(float)
        
        # --- 1. 处理 Token 转账（参考 monitor.py 的逻辑）---
        for tx_transfer in token_transfers:
            mint = tx_transfer.get('mint', '')
            token_amount = tx_transfer.get('tokenAmount', 0)
            
            # 🛡️ 特殊处理 WSOL：计入成本/收益，但不作为买卖目标
            if mint == self.wsol_mint:
                # Helius 的 tokenTransfers 通常已经是 Decimal 格式 (如 4.95)
                # 不需要除以 1e9，直接使用
                wsol_amount = float(token_amount)
                
                if tx_transfer.get('fromUserAccount') == self.target_wallet:
                    wsol_change -= wsol_amount
                elif tx_transfer.get('toUserAccount') == self.target_wallet:
                    wsol_change += wsol_amount
                continue
            
            # 处理其他代币（非 WSOL）
            # 其他代币的 tokenAmount 格式处理（通常已经是小数格式）
            # 注意：不同代币的 decimals 不同，但 Helius API 通常已经转换为小数格式
            if tx_transfer.get('fromUserAccount') == self.target_wallet:
                token_changes[mint] -= float(token_amount)
            elif tx_transfer.get('toUserAccount') == self.target_wallet:
                token_changes[mint] += float(token_amount)
        
        # --- 2. 处理 Native SOL 转账（参考 monitor.py 的逻辑）---
        sol_balance_change = 0
        
        for nt in native_transfers:
            amount = nt.get('amount', 0)  # 这是 lamports
            if nt.get('fromUserAccount') == self.target_wallet:
                sol_balance_change -= amount
            elif nt.get('toUserAccount') == self.target_wallet:
                sol_balance_change += amount
        
        # 转换为 SOL（lamports 转 SOL）
        native_sol_change = sol_balance_change / 1e9
        
        # --- 3. 合并 SOL/WSOL，避免重复计算（参考 monitor.py 的逻辑）---
        # 核心计算逻辑：取最大值防止双重计算
        # 场景 A (纯SOL买): Native花费 5, WSOL花费 0 -> Cost 5
        # 场景 B (Wrap+Swap): Native花费 5(去Wrap), WSOL花费 5(去Swap) -> Cost 5 (取 Max)
        # 场景 C (纯WSOL买): Native花费 0, WSOL花费 5 -> Cost 5
        # 注意：这里需要处理双向变动（正数和负数）
        sol_change = self._merge_sol_changes(native_sol_change, wsol_change)
        
        return sol_change, dict(token_changes), timestamp
    
    def _merge_sol_changes(self, native_sol: float, wsol: float) -> float:
        """
        合并原生 SOL 和 WSOL 变动，避免重复计算
        参考 monitor.py 的 parse_tx 逻辑：取最大值防止双重计算
        
        Args:
            native_sol: 原生 SOL 变动（正数表示增加，负数表示减少）
            wsol: WSOL 变动（正数表示增加，负数表示减少）
            
        Returns:
            合并后的 SOL 净变动
        """
        # 如果其中一个为 0，直接返回另一个
        if abs(native_sol) < 1e-9:
            return wsol
        if abs(wsol) < 1e-9:
            return native_sol
        
        # 🔥 核心计算逻辑：取最大值防止双重计算（参考 monitor.py）
        # 场景 A (纯SOL买): Native花费 -5, WSOL花费 0 -> Change -5
        # 场景 B (Wrap+Swap): Native花费 -5(去Wrap), WSOL花费 -5(去Swap) -> Change -5 (取 Max，即更负的)
        # 场景 C (纯WSOL买): Native花费 0, WSOL花费 -5 -> Change -5
        # 场景 D (纯SOL卖): Native收入 +5, WSOL收入 0 -> Change +5
        # 场景 E (Unwrap+Swap): Native收入 +5(从Unwrap), WSOL收入 +5(从Swap) -> Change +5 (取 Max，即更大的)
        
        if native_sol * wsol > 0:
            # 同向变动：可能是包装/解包操作，取绝对值较大的
            # 如果都是负数（支出），取绝对值较大的（即更负的，类似 monitor.py 的 max 逻辑）
            # 如果都是正数（收入），取较大的
            if native_sol < 0 and wsol < 0:
                # 都是支出，取绝对值较大的（即更负的）
                # 例如：-5 和 -3，取 -5（绝对值更大）
                return max(native_sol, wsol)
            else:
                # 都是收入，取较大的
                # 例如：+5 和 +3，取 +5
                return max(native_sol, wsol)
        else:
            # 反向变动：正常交易，直接相加
            # 例如：Native -5（支出），WSOL +3（收入），净变动 = -2
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
        # 添加超时保护：如果代币太多，限制查询时间
        max_price_queries = 30  # 最多查询30个代币的价格（减少以提升速度）
        if len(uncached_mints) > max_price_queries:
            logger.info(f"未缓存代币过多({len(uncached_mints)}个)，仅查询前{max_price_queries}个以提升速度")
            uncached_mints = uncached_mints[:max_price_queries]
        
        for i, mint in enumerate(uncached_mints):
            try:
                result = await self._get_single_token_price_sol(mint, max_retries)
                if result is not None and result > 0:
                    prices[mint] = result
                    self._price_cache[mint] = result
            except Exception as e:
                logger.debug(f"获取 {mint[:8]}... 价格失败: {e}")
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
                                    return price_sol
                            # out_amount为0，尝试下一个quote_amount
                            break
                        elif resp.status == 429:
                            # 429错误：尝试读取Retry-After头，否则使用指数退避
                            retry_after = resp.headers.get('Retry-After')
                            if retry_after:
                                try:
                                    wait_time = float(retry_after)
                                except (ValueError, TypeError):
                                    wait_time = min((attempt + 1) * 2, 60)  # 最多等待60秒
                            else:
                                # 指数退避：2秒、4秒、8秒...最多60秒
                                wait_time = min(2 ** (attempt + 1), 60)
                            logger.warning(f"Jupiter API rate limited (429), waiting {wait_time}s before retry")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # 非200状态码，记录日志但不重试（除非是最后一次尝试）
                            logger.debug(f"Jupiter API returned status {resp.status} for {token_mint[:8]}...")
                            if attempt < max_retries - 1:
                                continue
                            else:
                                break
                except asyncio.TimeoutError:
                    # 超时错误，记录日志但不等待（除非是最后一次尝试）
                    logger.debug(f"Jupiter API timeout for {token_mint[:8]}...")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        break
                except Exception as e:
                    logger.debug(f"Jupiter API error for {token_mint[:8]}...: {e}")
                    if attempt < max_retries - 1:
                        continue
                    else:
                        break
        
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
    
    def __init__(self, helius_api_key: str = None, db_manager: Optional[TransactionDBManager] = None):
        """
        初始化钱包分析器
        
        Args:
            helius_api_key: Helius API 密钥
            db_manager: 交易记录数据库管理器（可选）
        """
        self.helius_api_key = helius_api_key or HELIUS_API_KEY
        if not self.helius_api_key:
            raise ValueError("HELIUS_API_KEY 未配置")
        self.db_manager = db_manager
    
    async def fetch_history_pagination(
        self,
        session: aiohttp.ClientSession,
        address: str,
            max_count: int = 3000,
            helius_api_key=None
    ) -> List[dict]:
        """
        分页获取钱包交易历史（支持数据库缓存和智能分页）
        
        策略：
        1. 先从数据库查询缓存
        2. 逐页拉取Helius最新数据，检测重叠
        3. 如果重叠但数据不足，向后拉更老的数据
        
        Args:
            session: aiohttp 会话对象
            address: 钱包地址
            max_count: 最大获取数量
            helius_api_key: Helius API Key
            
        Returns:
            交易列表（按时间倒序，最新的在前）
        """
        page_size = 100
        retry_count = 0
        max_retries = 5
        
        # 1. 从数据库读取缓存
        cached_txs = []
        cached_signatures = set()
        need_fetch_new = True  # 是否需要拉取新数据
        
        if self.db_manager:
            cached_txs = self.db_manager.get_transactions(address, limit=max_count)
            cached_signatures = {tx.get('signature') for tx in cached_txs if tx.get('signature')}
            logger.debug(f"从数据库读取到 {len(cached_txs)} 条缓存交易: {address[:8]}...")
            
            # 检查缓存数据是否足够新且数量足够
            if cached_txs:
                # 获取最新交易的时间戳（第一条是最新的）
                latest_tx = cached_txs[0]
                latest_timestamp = latest_tx.get('timestamp', 0)
                
                if latest_timestamp > 0:
                    current_time = datetime.now().timestamp()
                    time_diff = current_time - latest_timestamp
                    hours_ago = time_diff / 3600
                    
                    # 如果最新交易在24小时内，且数据量足够，则不需要拉取新数据
                    if time_diff < 86400 and len(cached_txs) >= max_count:
                        # logger.info(f"缓存数据足够新（{hours_ago:.1f}小时前）且数量足够（{len(cached_txs)}条），跳过Helius API调用: {address[:8]}...")
                        need_fetch_new = False
                    elif time_diff < 86400 and len(cached_txs) < max_count:
                        # logger.info(f"缓存数据足够新（{hours_ago:.1f}小时前）但数量不足（{len(cached_txs)}/{max_count}），需要向后拉取更老的数据: {address[:8]}...")
                        need_fetch_new = False  # 不需要拉取新数据，只需要向后拉取
                    # else:
                        # logger.debug(f"缓存数据较旧（{hours_ago:.1f}小时前），需要拉取最新数据: {address[:8]}...")
        
        # 2. 逐页拉取Helius最新数据（如果需要）
        new_txs = []
        last_signature = None
        overlap_found = False
        
        # 如果不需要拉取新数据，直接跳到向后拉取逻辑
        if not need_fetch_new:
            # 如果数据量足够，直接返回缓存数据
            if len(cached_txs) >= max_count:
                return cached_txs[:max_count]
            # 否则需要向后拉取更老的数据
            overlap_found = True
        else:
            # 需要拉取最新数据
            while len(new_txs) < max_count:
                url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
                params = {
                            "api-key": helius_api_key,
                            "limit": page_size
                }
                if last_signature:
                    params["before"] = last_signature

                try:
                    async with session.get(url, params=params) as resp:
                        if resp.status == 429:
                            retry_count += 1
                            if retry_count > max_retries:
                                logger.warning(f"Helius API rate limit exceeded after {max_retries} retries, stopping at {len(new_txs)} transactions")
                                break
                            # 尝试读取Retry-After头，否则使用指数退避
                            retry_after = resp.headers.get('Retry-After')
                            if retry_after:
                                try:
                                    wait_time = float(retry_after)
                                except (ValueError, TypeError):
                                    wait_time = min(retry_count * 2, 60)  # 最多等待60秒
                            else:
                                # 指数退避：2秒、4秒、8秒...最多60秒
                                wait_time = min(2 ** retry_count, 60)
                            logger.warning(f"Helius API rate limited (429), waiting {wait_time}s before retry ({retry_count}/{max_retries})")
                            await asyncio.sleep(wait_time)
                            continue

                        if resp.status != 200:
                            logger.warning(f"Helius API returned status {resp.status}, stopping")
                            break

                        data = await resp.json()
                        if not data:
                            break

                        # 检测重叠
                        page_overlap = False
                        for tx in data:
                            sig = tx.get('signature')
                            if sig and sig in cached_signatures:
                                page_overlap = True
                                overlap_found = True
                                break

                        # 添加新交易（去重）
                        for tx in data:
                            sig = tx.get('signature')
                            if sig and sig not in cached_signatures:
                                new_txs.append(tx)
                                cached_signatures.add(sig)

                        # 如果发现重叠，说明最新数据已经拉够了
                        if page_overlap:
                            logger.debug(f"发现重叠，停止拉取新数据: {address[:8]}... (已拉取 {len(new_txs)} 条新交易)")
                            break

                        if len(data) < page_size:
                            break

                        last_signature = data[-1].get('signature')
                        retry_count = 0

                except aiohttp.ClientError as e:
                    logger.error(f"Network error fetching transactions: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error fetching transactions: {e}")
                    break
        
        # 3. 合并新数据和缓存
        all_txs = new_txs + cached_txs
        older_txs = []  # 向后拉取的更老数据
        
        # 4. 如果出现重叠但数据量不足，向后拉更老的数据
        if overlap_found and len(all_txs) < max_count:
            # 计算需要跳过的页数
            pages_to_skip = len(cached_txs) // page_size
            if pages_to_skip > 0:
                logger.debug(f"数据不足，向后拉取更老的数据: {address[:8]}... (跳过 {pages_to_skip} 页，已有 {len(cached_txs)} 条)")
                
                # 找到缓存中最老的交易signature作为起点
                if cached_txs:
                    oldest_signature = cached_txs[-1].get('signature')
                    if oldest_signature:
                        last_signature = oldest_signature
                        retry_count = 0
                        
                        while len(all_txs) + len(older_txs) < max_count:
                            url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
                            params = {
                                "api-key": helius_api_key,
                                "limit": page_size,
                                "before": last_signature
                            }
                            
                            try:
                                async with session.get(url, params=params) as resp:
                                    if resp.status == 429:
                                        retry_count += 1
                                        if retry_count > max_retries:
                                            break
                                        retry_after = resp.headers.get('Retry-After')
                                        if retry_after:
                                            try:
                                                wait_time = float(retry_after)
                                            except (ValueError, TypeError):
                                                wait_time = min(retry_count * 2, 60)
                                        else:
                                            wait_time = min(2 ** retry_count, 60)
                                        logger.warning(f"Helius API rate limited (429), waiting {wait_time}s")
                                        await asyncio.sleep(wait_time)
                                        continue
                                    
                                    if resp.status != 200:
                                        break
                                    
                                    data = await resp.json()
                                    if not data:
                                        break
                                    
                                    # 添加新交易（去重）
                                    for tx in data:
                                        sig = tx.get('signature')
                                        if sig and sig not in cached_signatures:
                                            older_txs.append(tx)
                                            cached_signatures.add(sig)
                                    
                                    if len(data) < page_size:
                                        break
                                    
                                    last_signature = data[-1].get('signature')
                                    retry_count = 0
                                    
                                    if len(all_txs) + len(older_txs) >= max_count:
                                        break
                            
                            except Exception as e:
                                logger.error(f"Error fetching older transactions: {e}")
                                break
                        
                        # 将更老的交易添加到末尾
                        all_txs.extend(older_txs)
        
        # 5. 限制返回数量并去重
        seen = set()
        unique_txs = []
        for tx in all_txs:
            sig = tx.get('signature')
            if sig and sig not in seen:
                seen.add(sig)
                unique_txs.append(tx)
            if len(unique_txs) >= max_count:
                break
        
        # 6. 保存新数据到数据库
        if self.db_manager:
            # 保存新拉取的数据
            if new_txs:
                self.db_manager.save_transactions(address, new_txs)
            # 如果有向后拉取的数据，也保存
            if older_txs:
                self.db_manager.save_transactions(address, older_txs)
        
        return unique_txs[:max_count]
    
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
        
        # 项目数据：{mint: {buy_sol, sell_sol, buy_tokens, sell_tokens, hold_periods, transactions}}
        # hold_periods: 持仓周期列表，每个周期包含 [start_time, end_time]
        # 用于正确计算持仓时间（同一代币可能有多个交易周期）
        projects = defaultdict(lambda: {
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "buy_tokens": 0.0,
            "sell_tokens": 0.0,
            "hold_periods": [],  # 持仓周期列表：[[start_time, end_time], ...]
            "current_position": 0.0,  # 当前持仓数量
            "current_period_start": 0,  # 当前持仓周期的开始时间
            "transactions": [],  # 记录每笔交易的详细信息
            "buy_count": 0,  # 买入次数
            "sell_count": 0  # 卖出次数
        })
        
        # 按时间正序处理交易（从最早到最新），这样才能正确跟踪持仓状态
        # 注意：transactions 可能是倒序的（最新的在前），需要先排序
        sorted_transactions = sorted(transactions, key=lambda x: x.get('timestamp', 0))
        for tx in sorted_transactions:
            # 1. 快速过滤：如果这笔交易在 API 层面就没有 tokenTransfers 且没有 nativeTransfers，直接跳过
            if not tx.get('tokenTransfers') and not tx.get('nativeTransfers'):
                continue

            try:
                # 解析交易
                sol_change, token_changes, timestamp = parser.parse_transaction(tx)
                
                # 计算归因
                buy_attributions, sell_attributions = attribution_calc.calculate_attribution(
                    sol_change, token_changes
                )
                
                # 更新项目数据
                for mint, delta in token_changes.items():
                    # 跳过 delta 为 0 的情况（同一笔交易中买入和卖出数量相等）
                    if abs(delta) < 1e-9:
                        continue
                    
                    # 更新代币数量
                    if delta > 0:
                        projects[mint]["buy_tokens"] += delta
                    else:
                        projects[mint]["sell_tokens"] += abs(delta)
                    
                    # 更新 SOL 成本/收益
                    if mint in buy_attributions:
                        projects[mint]["buy_sol"] += buy_attributions[mint]
                        # 统计买入次数（只有当买入金额大于0时才计数）
                        if buy_attributions[mint] > 1e-9:
                            projects[mint]["buy_count"] += 1
                    if mint in sell_attributions:
                        projects[mint]["sell_sol"] += sell_attributions[mint]
                        # 统计卖出次数（只有当卖出金额大于0时才计数）
                        if sell_attributions[mint] > 1e-9:
                            projects[mint]["sell_count"] += 1
                    
                    # 跟踪持仓周期（用于正确计算持仓时间）
                    prev_position = projects[mint]["current_position"]
                    new_position = prev_position + delta
                    projects[mint]["current_position"] = new_position
                    
                    # 如果持仓从0变为>0，开始新的持仓周期
                    if prev_position == 0 and new_position > 0 and timestamp > 0:
                        projects[mint]["current_period_start"] = timestamp
                    
                    # 如果持仓从>0变为0，结束当前持仓周期
                    elif prev_position > 0 and new_position == 0 and timestamp > 0:
                        period_start = projects[mint]["current_period_start"]
                        if period_start > 0:
                            # 如果开始时间和结束时间相同（同一笔交易中买入并卖出），至少记录1秒的持仓时间
                            end_time = timestamp
                            if end_time <= period_start:
                                end_time = period_start + 1  # 至少1秒
                            projects[mint]["hold_periods"].append([period_start, end_time])
                            projects[mint]["current_period_start"] = 0
                    
                    # 特殊情况：如果同一笔交易中同时买入和卖出（delta 可能很小但不为0）
                    # 这种情况下，如果持仓从0变为>0再变为0，需要特殊处理
                    # 但这种情况已经在上面处理了，因为我们会先处理买入（delta > 0），再处理卖出（delta < 0）
                    
                    # 记录交易详情
                    projects[mint]["transactions"].append({
                        "timestamp": timestamp,
                        "sol_change": sol_change,
                        "token_delta": delta,
                        "buy_sol": buy_attributions.get(mint, 0),
                        "sell_sol": sell_attributions.get(mint, 0)
                    })
                
                # 处理无 SOL 交易的跨代币兑换
                # 注意：跨代币兑换也需要更新持仓周期
                if abs(sol_change) < 1e-9 and token_changes:
                    for mint, delta in token_changes.items():
                        if delta > 0:
                            projects[mint]["buy_tokens"] += delta
                        else:
                            projects[mint]["sell_tokens"] += abs(delta)
                        
                        # 跟踪持仓周期（与上面相同的逻辑）
                        prev_position = projects[mint]["current_position"]
                        new_position = prev_position + delta
                        projects[mint]["current_position"] = new_position
                        
                        # 如果持仓从0变为>0，开始新的持仓周期
                        if prev_position == 0 and new_position > 0 and timestamp > 0:
                            projects[mint]["current_period_start"] = timestamp
                        
                        # 如果持仓从>0变为0，结束当前持仓周期
                        elif prev_position > 0 and new_position == 0 and timestamp > 0:
                            period_start = projects[mint]["current_period_start"]
                            if period_start > 0:
                                # 如果开始时间和结束时间相同（同一笔交易中买入并卖出），至少记录1秒的持仓时间
                                end_time = timestamp
                                if end_time <= period_start:
                                    end_time = period_start + 1  # 至少1秒
                                projects[mint]["hold_periods"].append([period_start, end_time])
                                projects[mint]["current_period_start"] = 0
                            
            except Exception as e:
                logger.warning(f"Error parsing transaction: {e}")
                continue
        
        # 获取当前价格并计算最终收益
        active_mints = [
            m for m, v in projects.items()
            if (v["buy_tokens"] - v["sell_tokens"]) > 0 and v["buy_sol"] >= MIN_COST_THRESHOLD
        ]
        
        # 优化：如果持仓代币太多，只查询前30个（避免查询时间过长）
        # 减少到30个以提升速度，因为每个代币查询需要3秒超时
        if len(active_mints) > 30:
            logger.info(f"持仓代币过多({len(active_mints)}个)，仅查询前30个的价格以提升速度")
            active_mints = active_mints[:30]

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
            
            remaining_tokens = max(0.0, data["buy_tokens"] - data["sell_tokens"])
            price_sol = prices_sol.get(mint, 0)
            
            # 计算收益
            if price_sol == 0 and remaining_tokens > 0:
                unrealized_sol = 0
            else:
                unrealized_sol = remaining_tokens * price_sol
            
            total_value_sol = data["sell_sol"] + unrealized_sol
            net_profit = total_value_sol - data["buy_sol"]
            roi = (total_value_sol / data["buy_sol"] - 1) if data["buy_sol"] > 0 else 0
            
            # 计算持仓时间（累加所有持仓周期的时间）
            hold_time_minutes = 0.0
            hold_periods = data.get("hold_periods", [])
            current_period_start = data.get("current_period_start", 0)
            current_position = data.get("current_position", 0)
            
            # 累加已完成的持仓周期
            for period_start, period_end in hold_periods:
                if period_start > 0 and period_end > 0:
                    hold_time_minutes += (period_end - period_start) / 60
            
            # 如果有未完成的持仓周期（当前仍有持仓），使用当前时间作为结束时间
            if current_period_start > 0 and remaining_tokens > 0:
                current_time = datetime.now().timestamp()
                hold_time_minutes += (current_time - current_period_start) / 60
            
            # 特殊情况：如果代币已经清仓（remaining_tokens == 0），但还有未记录的持仓周期
            # 这可能发生在最后一笔交易清仓时，current_period_start 还没有被记录到 hold_periods
            if remaining_tokens == 0 and current_period_start > 0:
                # 从交易记录中找到最后一笔交易的时间作为结束时间
                if data.get("transactions"):
                    last_tx_time = max(tx.get("timestamp", 0) for tx in data["transactions"])
                    if last_tx_time >= current_period_start:  # 使用 >= 而不是 >，允许相同时间
                        # 如果开始时间和结束时间相同，至少记录1秒的持仓时间
                        end_time = last_tx_time
                        if end_time <= current_period_start:
                            end_time = current_period_start + 1  # 至少1秒
                        hold_time_minutes += (end_time - current_period_start) / 60
                        # 也添加到 hold_periods 以便计算 first_time 和 last_time
                        hold_periods.append([current_period_start, end_time])
                        # 清空 current_period_start，因为已经记录到 hold_periods 了
                        current_period_start = 0
            
            # 如果持仓时间为0，但代币有交易记录，说明可能是同一笔交易中买入并卖出
            # 这种情况下，至少应该记录一个很小的持仓时间（比如1秒）
            if hold_time_minutes == 0 and data.get("transactions") and len(data["transactions"]) > 0:
                # 检查是否有买入和卖出
                has_buy = any(tx.get("token_delta", 0) > 0 for tx in data["transactions"])
                has_sell = any(tx.get("token_delta", 0) < 0 for tx in data["transactions"])
                if has_buy and has_sell:
                    # 同一代币有买入和卖出，至少记录1秒的持仓时间
                    tx_times = [tx.get("timestamp", 0) for tx in data["transactions"] if tx.get("timestamp", 0) > 0]
                    if tx_times:
                        min_time = min(tx_times)
                        max_time = max(tx_times)
                        if max_time > min_time:
                            hold_time_minutes = (max_time - min_time) / 60
                        else:
                            hold_time_minutes = 1.0 / 60  # 至少1秒
                        # 也添加到 hold_periods
                        if not hold_periods:
                            hold_periods.append([min_time, max_time if max_time > min_time else min_time + 1])
            
            # 为了兼容性，保留 first_time 和 last_time（用于时间窗口分析）
            first_time = 0
            last_time = 0
            if hold_periods:
                first_time = min(period[0] for period in hold_periods)
                last_time = max(period[1] for period in hold_periods)
            if current_period_start > 0 and remaining_tokens > 0:
                if first_time == 0 or current_period_start < first_time:
                    first_time = current_period_start
                current_time = datetime.now().timestamp()
                if last_time == 0 or current_time > last_time:
                    last_time = current_time
            
            # 如果所有持仓周期都已结束，但从交易记录中获取时间范围（作为后备方案）
            # 这确保 first_time 和 last_time 总是有值（用于时间窗口分析）
            if data.get("transactions"):
                tx_times = [tx.get("timestamp", 0) for tx in data["transactions"] if tx.get("timestamp", 0) > 0]
                if tx_times:
                    tx_first = min(tx_times)
                    tx_last = max(tx_times)
                    # 如果 first_time 或 last_time 为 0，使用交易记录中的时间
                    if first_time == 0:
                        first_time = tx_first
                    elif tx_first < first_time:
                        first_time = tx_first  # 使用更早的时间
                    if last_time == 0:
                        last_time = tx_last
                    elif tx_last > last_time:
                        last_time = tx_last  # 使用更晚的时间
            
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
                "first_time": first_time,  # 使用计算出的 first_time
                "last_time": last_time,  # 使用计算出的 last_time
                "transactions": data["transactions"],
                "has_price": price_sol > 0,
                "remaining_tokens": remaining_tokens,  # 剩余代币数量
                "unrealized_sol": unrealized_sol,  # 未实现收益（SOL）
                "unsettled_cost": unsettled_cost,  # 未结算部分的成本
                "is_unsettled": remaining_tokens > 0,  # 是否未结算
                "buy_count": data.get("buy_count", 0),  # 买入次数
                "sell_count": data.get("sell_count", 0)  # 卖出次数
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
            profit_pct_excluding_max = (
                    profit_excluding_max / cost_excluding_max * 100) if cost_excluding_max > 0 else 0
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
        
        # 2. 归零战神：胜率 >= 90% 且最大亏损 <= -95%（已移除，不加入黑名单）
        # if win_rate >= ZERO_WARRIOR_WIN_RATE and max_loss <= ZERO_WARRIOR_MAX_LOSS:
        #     flags["is_trash"] = True
        #     flags["reasons"].append("归零战神：胜率高但一输就归零")

        # 3. 内幕狗：只交易过 1-2 个代币（已移除，不加入黑名单，万一以后会变强）
        # if unique_tokens <= INSIDER_DOG_MAX_TOKENS:
        #     flags["is_trash"] = True
        #     flags["reasons"].append(f"内幕狗：只交易过 {unique_tokens} 个代币")

        # 4. 交易超过5个代币但目前仍然处于亏损
        if unique_tokens > 5 and total_profit < 0:
            flags["is_trash"] = True
            flags["reasons"].append(f"交易{unique_tokens}个代币但仍亏损 {total_profit:.2f} SOL")

        # 5. 亏损>95%的代币占比总交易代币数大于10%
        if unique_tokens > 0:
            # 统计亏损<=-95%的代币数量
            severe_losses = [r for r in losses if r.get('roi', 0) <= -0.95]
            severe_loss_count = len(severe_losses)
            # 计算占比
            severe_loss_ratio = severe_loss_count / unique_tokens if unique_tokens > 0 else 0
            # 如果占比 > 10%，则认为是垃圾地址
            if severe_loss_ratio > 0.10:
                flags["is_trash"] = True
                flags["reasons"].append(f"亏损>95%的代币占比{severe_loss_ratio:.1%}({severe_loss_count}/{unique_tokens})超过10%")

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
    
    # 初始化数据库管理器（支持缓存）
    db_manager = TransactionDBManager()
    analyzer = WalletAnalyzerV2(db_manager=db_manager)
    
    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V2 (超严格版): {args.wallet[:6]}...")
        txs = await analyzer.fetch_history_pagination(session, args.wallet, args.max_txs, analyzer.helius_api_key)
        
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
        
        # 计算平均每次买入的SOL数量
        all_buy_amounts = []
        for r in results:
            transactions = r.get("transactions", [])
            for tx in transactions:
                buy_sol = tx.get("buy_sol", 0)
                if buy_sol > 1e-9:  # 只统计有效的买入金额
                    all_buy_amounts.append(buy_sol)
        avg_buy_sol = sum(all_buy_amounts) / len(all_buy_amounts) if all_buy_amounts else 0

        # 计算已清仓代币的平均买入次数和卖出次数
        settled_tokens = [r for r in results if not r.get('is_unsettled', False) and r.get('remaining_tokens', 0) == 0]
        if settled_tokens:
            avg_buy_count = sum(r.get('buy_count', 0) for r in settled_tokens) / len(settled_tokens)
            avg_sell_count = sum(r.get('sell_count', 0) for r in settled_tokens) / len(settled_tokens)
        else:
            avg_buy_count = 0
            avg_sell_count = 0

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
        print(f"   • 平均每次买入: {avg_buy_sol:.3f} SOL")
        print(f"   • 已清仓代币平均买入次数: {avg_buy_count:.2f} 次")
        print(f"   • 已清仓代币平均卖出次数: {avg_sell_count:.2f} 次")
        
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
            print(
                f" {status_icon} {token_short} | 利润 {profit:>+8.2f} SOL | ROI {roi_pct:>+7.1f}% | 持仓 {hold_time:>6.1f} 分钟")


if __name__ == "__main__":
    asyncio.run(main())
