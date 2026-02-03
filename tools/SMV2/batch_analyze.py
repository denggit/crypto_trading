#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : batch_analyze.py
@Description: 批量钱包分析工具 V2 (超严格版)
              - 批量分析多个钱包地址
              - 自动黑名单过滤低质量钱包
              - 导出 Excel 报告（包含详细评分和定位）
              - 改进错误处理和日志记录
@Author     : Auto-generated
@Date       : 2026-02-02
"""
import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm

from tools.SMV2.key_list import HELIUS_KEY_LIST, JUPITER_KEY_LIST

# 确保能找到 analyze_wallet 模块
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir))

try:
    from analyze_wallet import WalletAnalyzerV2, WalletScorerV2
except ImportError:
    print("❌ 错误：找不到 analyze_wallet 模块")
    sys.exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === ⚙️ 配置常量 ===
TOOLS_DIR = Path(__file__).parent.parent
TRASH_FILE = str(TOOLS_DIR / "wallets_trash.txt")
WALLETS_FILE = str(TOOLS_DIR / "wallets_check.txt")
RESULTS_DIR = str(Path(__file__).parent / "results")
CONCURRENT_LIMIT = 5  # 并发限制
DUST_THRESHOLD = 0.01  # 粉尘阈值：未实现收益低于此值的代币视为粉尘


class APIKeyManager:
    """
    API Key 管理器：负责管理多个 API Key，允许并行使用，但同一 Key 间隔至少1秒
    
    职责：
    - 为每个 Key 创建独立的锁，允许不同 Key 并行使用
    - 跟踪每个 Key 的最后调用时间
    - 确保同一 Key 的调用间隔至少1秒
    """

    def __init__(self, key_list: List[str], api_name: str = "API"):
        """
        初始化 API Key 管理器
        
        Args:
            key_list: API Key 列表
            api_name: API 名称（用于日志）
        """
        if not key_list:
            raise ValueError(f"{api_name} Key 列表不能为空")
        self.key_list = [k for k in key_list if k and k.strip()]  # 过滤空值
        if not self.key_list:
            raise ValueError(f"{api_name} Key 列表中没有有效的 Key")
        self.api_name = api_name
        # 为每个 Key 创建独立的锁和调用时间跟踪
        self.key_locks: Dict[str, asyncio.Lock] = {key: asyncio.Lock() for key in self.key_list}
        self.last_call_times: Dict[str, float] = {}  # {key: last_call_timestamp}
        self.current_index = 0
        self._index_lock = asyncio.Lock()  # 用于轮询选择Key的锁
        logger.info(f"初始化 {api_name} Key 管理器: {len(self.key_list)} 个 Keys（支持并行）")

    async def get_key_and_lock(self) -> Tuple[str, asyncio.Lock]:
        """
        获取下一个可用的 API Key 和对应的锁（确保间隔至少1秒）
        
        Returns:
            (key, lock): 可用的 API Key 和对应的锁
        """
        import time
        async with self._index_lock:
            current_time = time.time()

            # 尝试找到可用的 Key（距离上次调用至少1秒）
            for _ in range(len(self.key_list)):
                key = self.key_list[self.current_index]
                last_call = self.last_call_times.get(key, 0)
                elapsed = current_time - last_call

                if elapsed >= 1.0:
                    # 这个 Key 可用，更新调用时间并返回
                    self.last_call_times[key] = current_time
                    self.current_index = (self.current_index + 1) % len(self.key_list)
                    return key, self.key_locks[key]

                # 这个 Key 不可用，尝试下一个
                self.current_index = (self.current_index + 1) % len(self.key_list)

            # 如果所有 Key 都不可用，等待最短的时间
            if self.last_call_times:
                wait_times = [1.0 - (current_time - last_call)
                              for last_call in self.last_call_times.values()
                              if (current_time - last_call) < 1.0]
                if wait_times:
                    min_wait = min(wait_times)
                    if min_wait > 0:
                        await asyncio.sleep(min_wait)
                        current_time = time.time()

            # 再次尝试获取 Key（此时应该至少有一个可用）
            key = self.key_list[self.current_index]
            self.last_call_times[key] = current_time
            self.current_index = (self.current_index + 1) % len(self.key_list)
            return key, self.key_locks[key]


def is_valid_solana_address(address: str) -> bool:
    """
    验证是否为有效的 Solana 钱包地址
    
    Args:
        address: 待验证的地址字符串
        
    Returns:
        是否为有效的 Solana 地址
    """
    if not address or not isinstance(address, str):
        return False

    # Solana 地址长度通常在 32-44 位，使用 Base58 字符集
    if not (32 <= len(address) <= 44):
        return False

    # Base58 字符集：不包含 0, O, I, l
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', address):
        return False

    # 排除系统地址
    if address == "So11111111111111111111111111111111111111111":
        return False

    return True


class WalletListSaver:
    """
    钱包列表保存器：负责将有效的钱包地址保存回文件
    """

    @staticmethod
    def save_valid_addresses(
            addresses: List[str],
            wallets_file: str = WALLETS_FILE
    ) -> bool:
        """
        保存有效的钱包地址到文件（去重、验证格式）
        
        Args:
            addresses: 钱包地址列表
            wallets_file: 钱包列表文件路径
            
        Returns:
            是否成功保存
        """
        if not addresses:
            logger.warning("没有地址需要保存")
            return False

        try:
            # 验证并去重
            valid_addresses = set()
            for addr in addresses:
                addr = addr.strip()
                if addr and is_valid_solana_address(addr):
                    valid_addresses.add(addr)

            if not valid_addresses:
                logger.warning("没有有效的钱包地址需要保存")
                return False

            # 排序并保存
            sorted_addresses = sorted(list(valid_addresses))

            with open(wallets_file, 'w', encoding='utf-8') as f:
                for addr in sorted_addresses:
                    f.write(f"{addr}\n")

            logger.info(f"已保存 {len(sorted_addresses)} 个有效钱包地址到 {wallets_file}")
            return True

        except Exception as e:
            logger.error(f"保存钱包地址失败: {e}")
            return False


class TrashListManager:
    """
    黑名单管理器：负责管理低质量钱包黑名单
    """

    def __init__(self, trash_file: str = TRASH_FILE):
        """
        初始化黑名单管理器
        
        Args:
            trash_file: 黑名单文件路径
        """
        self.trash_file = trash_file
        self._trash_set: Optional[Set[str]] = None

    def load(self) -> Set[str]:
        """
        加载黑名单
        
        Returns:
            黑名单地址集合
        """
        if self._trash_set is not None:
            return self._trash_set

        if not os.path.exists(self.trash_file):
            self._trash_set = set()
            return self._trash_set

        try:
            with open(self.trash_file, 'r', encoding='utf-8') as f:
                self._trash_set = {line.strip() for line in f if line.strip()}
            logger.info(f"加载黑名单: {len(self._trash_set)} 个地址")
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")
            self._trash_set = set()

        return self._trash_set

    def add(self, address: str) -> bool:
        """
        添加地址到黑名单
        
        Args:
            address: 钱包地址
            
        Returns:
            是否成功添加
        """
        try:
            with open(self.trash_file, 'a', encoding='utf-8') as f:
                f.write(f"{address}\n")

            if self._trash_set is not None:
                self._trash_set.add(address)

            logger.debug(f"已添加地址到黑名单: {address[:6]}...")
            return True
        except Exception as e:
            logger.error(f"添加黑名单失败: {e}")
            return False

    def contains(self, address: str) -> bool:
        """
        检查地址是否在黑名单中
        
        Args:
            address: 钱包地址
            
        Returns:
            是否在黑名单中
        """
        if self._trash_set is None:
            self.load()
        return address in (self._trash_set or set())


class WalletListLoader:
    """
    钱包列表加载器：负责从文件加载钱包地址列表
    """

    @staticmethod
    def load(wallets_file: str = WALLETS_FILE) -> List[str]:
        """
        从文件加载钱包地址列表
        
        Args:
            wallets_file: 钱包列表文件路径
            
        Returns:
            钱包地址列表
        """
        if not os.path.exists(wallets_file):
            logger.error(f"找不到钱包列表文件: {wallets_file}")
            return []

        try:
            with open(wallets_file, 'r', encoding='utf-8') as f:
                addresses = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                ]
                addresses = list(set(addresses))
            logger.info(f"从 {wallets_file} 加载了 {len(addresses)} 个地址")
            return addresses
        except Exception as e:
            logger.error(f"加载钱包列表失败: {e}")
            return []


class BatchAnalyzerV2:
    """
    批量分析器 V2：负责批量分析多个钱包（超严格版）
    
    职责：
    - 并发分析多个钱包（数据处理并发，API调用串行）
    - 自动过滤低质量钱包（垃圾地址）
    - 生成详细分析报告
    
    设计：
    - 使用生产者-消费者模式
    - API调用（Helius/Jupiter）串行化，避免并发调用
    - 数据处理（解析、评分计算）可以并发
    """

    def __init__(
            self,
            analyzer: WalletAnalyzerV2,
            trash_manager: TrashListManager,
            helius_key_manager: APIKeyManager,
            jupiter_key_manager: APIKeyManager,
            concurrent_limit: int = CONCURRENT_LIMIT
    ):
        """
        初始化批量分析器
        
        Args:
            analyzer: 钱包分析器实例
            trash_manager: 黑名单管理器实例
            helius_key_manager: Helius API Key 管理器
            jupiter_key_manager: Jupiter API Key 管理器
            concurrent_limit: 数据处理并发限制（API调用始终串行）
        """
        self.analyzer = analyzer
        self.trash_manager = trash_manager
        self.helius_key_manager = helius_key_manager
        self.jupiter_key_manager = jupiter_key_manager
        self.concurrent_limit = concurrent_limit
        # 数据处理并发控制
        self.data_processing_semaphore = asyncio.Semaphore(concurrent_limit)
        # 移除全局api_lock，改为每个Key独立的锁（允许N个Key并行，N=key数量）

    async def analyze_one_wallet(
            self,
            session: aiohttp.ClientSession,
            address: str,
            pbar: tqdm,
            max_txs: int = 5000
    ) -> Optional[Dict]:
        """
        分析单个钱包（生产者-消费者模式）
        
        Args:
            session: aiohttp 会话对象
            address: 钱包地址
            pbar: 进度条对象
            max_txs: 最大交易数量
            
        Returns:
            分析结果字典，如果失败或应过滤则返回 None
        """
        try:
            # === 阶段1：API调用（允许N个Key并行，N=key数量，但同一Key内部串行）===
            # 获取可用的Helius Key和对应的锁（确保同一Key间隔1秒）
            helius_key, helius_lock = await self.helius_key_manager.get_key_and_lock()
            async with helius_lock:
                # 1. 拉取交易数据（Helius API）
                try:
                    txs = await self.analyzer.fetch_history_pagination(
                        session, address, max_txs, helius_api_key=helius_key
                    )
                except ValueError as e:
                    # API Key 未配置等配置错误
                    logger.error(f"配置错误: {e}")
                    pbar.update(1)
                    return None
                except aiohttp.ClientError as e:
                    # 网络错误（连接失败、超时等）
                    logger.warning(f"网络错误获取钱包 {address[:8]}... 交易数据: {e}")
                    pbar.update(1)
                    return None
                except Exception as e:
                    # 其他未知错误
                    logger.warning(f"获取钱包 {address[:8]}... 交易数据失败: {e}")
                    pbar.update(1)
                    return None

                # 如果返回空列表，可能是地址不存在（404），加入黑名单
                if txs == []:
                    logger.info(f"地址不存在或无效: {address[:8]}...，加入黑名单")
                    self.trash_manager.add(address)
                    pbar.update(1)
                    return None

                # 优化：如果交易数量太少（<10笔），可能不值得分析，提前退出
                if not txs or len(txs) < 10:
                    pbar.update(1)
                    return None

            # 2. 解析代币项目（内部会调用 Jupiter API）
            # 注意：Helius和Jupiter之间不需要间隔，只有同一API之间需要间隔
            # Jupiter API 的 Key 会在 PriceFetcher 内部通过 key_manager 获取
            try:
                analysis_result = await self.analyzer.parse_token_projects(
                    session, txs, address, jupiter_key_manager=self.jupiter_key_manager
                )
            except Exception as e:
                logger.warning(f"解析钱包 {address[:8]}... 代币项目失败: {e}")
                pbar.update(1)
                return None

            # 优化：如果有效项目太少，提前退出
            results = analysis_result.get("results", [])
            if not results or len(results) < 3:
                pbar.update(1)
                return None

            # === 阶段2：数据处理（可以并发）===
            # 使用数据处理信号量控制并发数，但可以多个任务同时处理
            async with self.data_processing_semaphore:
                # 3. 计算评分（纯计算，无API调用）
                scores = WalletScorerV2.calculate_scores(analysis_result)

                # 4. 自动黑名单过滤（垃圾地址）
                if scores["flags"].get("is_trash", False):
                    self.trash_manager.add(address)
                    pbar.update(1)
                    return None

                # 5. 提取详细指标（纯数据处理）
                results = analysis_result["results"]
                dims = scores["dimensions"]
                profit_dim = dims["profit"]
                persistence_dim = dims["persistence"]
                authenticity_dim = dims["authenticity"]
                positioning = scores["positioning"]

                # 6. 提取最佳定位
                best_role = "未知"
                best_role_score = 0
                if positioning:
                    best_role = max(positioning, key=positioning.get)
                    best_role_score = positioning[best_role]

                # 7. 计算基础指标
                wins = [r for r in results if r.get('is_win', False)]
                losses = [r for r in results if not r.get('is_win', False)]
                win_rate = len(wins) / len(results) if results else 0
                total_profit = profit_dim.get("total_profit", 0)
                max_roi = profit_dim.get("max_roi", 0)

                # 8. 计算未结算token统计（排除粉尘）
                unsettled_tokens = [
                    r for r in results
                    if r.get('is_unsettled', False) and r.get('unrealized_sol', 0) >= DUST_THRESHOLD
                ]

                unsettled_count = len(unsettled_tokens)
                unsettled_profit = sum(r.get('unrealized_sol', 0) for r in unsettled_tokens)
                unsettled_hold_times = [r.get('hold_time', 0) for r in unsettled_tokens if r.get('hold_time', 0) > 0]
                unsettled_avg_hold_time = sum(unsettled_hold_times) / len(
                    unsettled_hold_times) if unsettled_hold_times else 0

                # 计算未结算token的总成本（用于计算ROI）
                # 使用未结算部分的成本，而不是总买入成本
                unsettled_cost = sum(r.get('unsettled_cost', 0) for r in unsettled_tokens)
                unsettled_roi = (unsettled_profit / unsettled_cost - 1) if unsettled_cost > 0 else 0

                # 9. 计算单币亏损超过95%的数量
                severe_loss_count = len([r for r in losses if r.get('roi', 0) <= -0.95])

                pbar.update(1)
                return {
                    "钱包地址": address,
                    "综合评分": scores["final_score"],
                    "战力评级": scores["tier"],
                    "最佳定位": best_role,
                    "定位评分": best_role_score,
                    "盈利力评分": profit_dim.get("score", 0),
                    "持久力评分": persistence_dim.get("score", 0),
                    "真实性评分": authenticity_dim.get("score", 0),
                    "盈亏比": round(profit_dim.get("profit_factor", 0), 2),
                    "胜率": round(win_rate, 3),
                    "总盈亏(SOL)": round(total_profit, 2),
                    "30天盈利(SOL)": round(profit_dim.get("profit_30d", 0), 2),
                    "30天盈利(%)": round(profit_dim.get("profit_pct_30d", 0), 2),
                    "7天盈利(SOL)": round(profit_dim.get("profit_7d", 0), 2),
                    "7天盈利(%)": round(profit_dim.get("profit_pct_7d", 0), 2),
                    "最大单笔ROI": f"{max_roi:.0%}",
                    "最大单笔亏损": f"{profit_dim.get('max_single_loss', 0):.1%}",
                    "平均持仓(分钟)": round(authenticity_dim.get("avg_hold_time", 0), 1),
                    "盈利持仓(分钟)": round(authenticity_dim.get("avg_win_hold_time", 0), 1),
                    "亏损持仓(分钟)": round(authenticity_dim.get("avg_loss_hold_time", 0), 1),
                    "代币多样性": authenticity_dim.get("unique_tokens", 0),
                    "30天代币数": persistence_dim.get("tokens_30d", 0),
                    "30天交易数": persistence_dim.get("tx_count_30d", 0),
                    "7天代币数": persistence_dim.get("tokens_7d", 0),
                    "7天交易数": persistence_dim.get("tx_count_7d", 0),
                    "项目总数": len(results),
                    "未结算token数": unsettled_count,
                    "未结算盈利(SOL)": round(unsettled_profit, 2),
                    "未结算ROI": f"{unsettled_roi:.1%}",
                    "未结算平均持仓(分钟)": round(unsettled_avg_hold_time, 1),
                    "单币亏损>95%数量": severe_loss_count,
                    "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "🛡️ 稳健中军": positioning.get("🛡️ 稳健中军", 0),
                    "⚔️ 土狗猎手": positioning.get("⚔️ 土狗猎手", 0),
                    "💎 钻石之手": positioning.get("💎 钻石之手", 0),
                    "🚀 短线高手": positioning.get("🚀 短线高手", 0),
                }

        except Exception as e:
            logger.error(f"分析钱包 {address[:8]}... 时出错: {e}", exc_info=True)
            pbar.update(1)
            return None

    async def analyze_batch(
            self,
            addresses: List[str],
            max_txs: int = 5000,
            save_interval: int = 20,
            exporter: 'ReportExporterV2' = None
    ) -> List[Dict]:
        """
        批量分析钱包列表（生产者-消费者模式）
        
        设计：
        - 所有任务并发创建（生产者）
        - API调用允许N个Key并行（N=key数量），但同一Key内部串行
        - 数据处理并发（通过data_processing_semaphore）
        - 每处理N个钱包自动保存一次报告
        
        Args:
            addresses: 钱包地址列表
            max_txs: 每个钱包最大交易数量（默认5000，降低以提升速度）
            save_interval: 每处理多少个钱包保存一次报告（默认20）
            exporter: 报告导出器实例（用于定期保存）
            
        Returns:
            分析结果列表
        """
        pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")

        helius_key_count = len(self.helius_key_manager.key_list)
        jupiter_key_count = len(self.jupiter_key_manager.key_list)
        logger.info(
            f"开始分析 {len(addresses)} 个钱包（Helius {helius_key_count}个Key并行，Jupiter {jupiter_key_count}个Key并行，数据处理并发{self.concurrent_limit}）...")
        logger.info(f"每成功分析 {save_interval} 个钱包自动保存一次报告（只统计成功的）")

        # 共享的结果列表和计数器（用于定期保存）
        all_results: List[Dict] = []
        completed_count = 0  # 成功分析的钱包数（只统计成功的）
        results_lock = asyncio.Lock()
        save_lock = asyncio.Lock()  # 保存操作的锁，确保同时只有一个保存任务
        save_tasks: List[asyncio.Task] = []  # 所有保存任务列表

        # 确保 exporter 在闭包中可用
        if exporter is None:
            logger.warning("⚠️ exporter 为 None，中间报告保存功能将被禁用")

        async def save_report_async(results_to_save: List[Dict], count: int):
            """
            异步保存报告（不阻塞主流程）
            
            Args:
                results_to_save: 要保存的结果列表（复制一份避免并发修改）
                count: 当前完成数量
            """
            async with save_lock:
                try:
                    logger.info(f"🔄 开始保存中间报告 ({count} 个钱包，结果数: {len(results_to_save)})...")

                    # 检查 exporter 是否存在
                    if exporter is None:
                        logger.error(f"❌ exporter 为 None，无法保存 ({count} 个钱包)")
                        return

                    # 直接调用 export 方法（不使用 run_in_executor，避免问题）
                    # 因为 pandas 操作很快，不需要放到线程池
                    try:
                        temp_file = exporter.export(
                            results_to_save.copy(),  # 复制一份避免并发修改
                            RESULTS_DIR,
                            True  # is_temp=True
                        )
                        if temp_file:
                            abs_path = os.path.abspath(temp_file)
                            # 检查文件是否真的存在
                            if os.path.exists(temp_file):
                                file_size = os.path.getsize(temp_file)
                                logger.info(f"✅ 已保存中间报告: {abs_path} ({count} 个钱包，文件大小: {file_size} 字节)")
                            else:
                                logger.error(f"❌ 文件保存失败: 文件不存在 {abs_path}")
                        else:
                            logger.warning(f"⚠️ 保存中间报告返回 None ({count} 个钱包)")
                    except Exception as export_error:
                        logger.error(f"❌ 调用 exporter.export 失败 ({count} 个钱包): {export_error}", exc_info=True)
                        raise
                except Exception as e:
                    logger.error(f"❌ 保存中间报告失败 ({count} 个钱包): {e}", exc_info=True)

        async def analyze_task(session, addr, index):
            """
            单个钱包分析任务（生产者）
            内部会通过锁控制API调用（每个Key独立锁），数据处理并发
            """
            nonlocal completed_count, save_tasks
            try:
                result = await self.analyze_one_wallet(session, addr, pbar, max_txs)

                if result is not None:
                    should_save = False
                    current_count = 0
                    async with results_lock:
                        all_results.append(result)
                        completed_count += 1  # 只统计成功的
                        current_count = completed_count  # 保存当前值，用于日志

                        # 每成功分析N个钱包保存一次报告（异步，不阻塞）
                        if completed_count % save_interval == 0:
                            if exporter:
                                should_save = True
                                logger.info(
                                    f"📝 触发保存任务: 成功分析 {completed_count} 个钱包，结果数: {len(all_results)}")
                            else:
                                logger.warning(
                                    f"⚠️ exporter 为 None，无法保存中间报告 (成功分析 {completed_count} 个钱包)")

                        # # 每成功分析10个钱包输出一次日志（更频繁，便于调试）
                        # if completed_count % 10 == 0:
                        #     logger.info(f"进度: 成功分析 {completed_count} 个钱包 ({100*completed_count/len(addresses):.1f}%)")

                        # 每成功分析50个钱包输出一次详细日志
                        if completed_count % 50 == 0:
                            logger.info(f"详细进度: 成功分析 {completed_count} 个钱包，结果数: {len(all_results)}")

                            # 清理价格缓存（每50个钱包清理一次）
                            # 注意：这里需要访问analyzer的price_fetcher，但它是每个钱包独立的
                            # 所以缓存清理在PriceFetcher内部自动进行

                    # 异步保存（不阻塞主流程）
                    if should_save:
                        logger.info(f"🔄 创建保存任务: 成功分析 {current_count} 个钱包，结果数: {len(all_results)}")
                        # 创建异步保存任务（不等待完成）
                        try:
                            task = asyncio.create_task(
                                save_report_async(all_results.copy(), current_count)
                            )
                            save_tasks.append(task)
                            logger.info(f"✅ 保存任务已创建，当前共有 {len(save_tasks)} 个保存任务")
                        except Exception as task_error:
                            logger.error(f"❌ 创建保存任务失败: {task_error}", exc_info=True)

                return result
            except Exception as e:
                logger.error(f"处理钱包 {addr[:8]}... 时出错: {e}")
                pbar.update(1)
                return None

        # 创建所有任务并发执行（生产者模式）
        # API调用会在内部通过每个Key的独立锁控制（允许N个Key并行，N=key数量）
        # 数据处理可以通过data_processing_semaphore并发
        async with aiohttp.ClientSession() as session:
            tasks = [analyze_task(session, addr, i) for i, addr in enumerate(addresses)]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            # 过滤掉异常和None（结果已经在analyze_task中添加到all_results）
            exception_count = 0
            for r in raw_results:
                if isinstance(r, Exception):
                    exception_count += 1
                    if exception_count <= 5:  # 只记录前5个异常
                        logger.error(f"任务执行异常: {r}")
            if exception_count > 5:
                logger.warning(f"还有 {exception_count - 5} 个异常未显示")

        # 等待所有保存任务完成
        if save_tasks:
            logger.info(f"等待 {len(save_tasks)} 个保存任务完成...")
            await asyncio.gather(*save_tasks, return_exceptions=True)
            logger.info("所有保存任务已完成")

        pbar.close()
        return all_results


class ReportExporterV2:
    """
    报告导出器 V2：负责导出分析结果到 Excel（包含详细评分）
    """

    @staticmethod
    def export(results: List[Dict], output_dir: str = RESULTS_DIR, is_temp: bool = False) -> Optional[str]:
        """
        导出分析结果到 Excel
        
        Args:
            results: 分析结果列表
            output_dir: 输出目录
            is_temp: 是否为临时文件（True则覆盖临时文件，False则创建新文件）
            
        Returns:
            输出文件路径，如果失败则返回 None
        """
        if not results:
            logger.warning(f"没有结果可导出 (results为空，长度: {len(results) if results else 0})")
            return None

        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)

        try:
            # 按综合评分排序
            df = pd.DataFrame(results).sort_values(by="综合评分", ascending=False)

            # 重新排列列的顺序，让重要信息在前面
            important_cols = [
                "钱包地址", "综合评分", "战力评级", "最佳定位", "定位评分",
                "盈利力评分", "持久力评分", "真实性评分",
                "盈亏比", "胜率", "总盈亏(SOL)", "30天盈利(SOL)", "30天盈利(%)",
                "7天盈利(SOL)", "7天盈利(%)", "最大单笔ROI", "最大单笔亏损",
                "平均持仓(分钟)", "盈利持仓(分钟)", "亏损持仓(分钟)",
                "代币多样性", "30天代币数", "30天交易数", "7天代币数", "7天交易数",
                "项目总数", "未结算token数", "未结算盈利(SOL)", "未结算ROI", "未结算平均持仓(分钟)",
                "单币亏损>95%数量",
                "🛡️ 稳健中军", "⚔️ 土狗猎手", "💎 钻石之手", "🚀 短线高手",
                "分析时间"
            ]

            # 确保所有列都存在
            available_cols = [col for col in important_cols if col in df.columns]
            remaining_cols = [col for col in df.columns if col not in available_cols]
            df = df[available_cols + remaining_cols]

            if is_temp:
                # 临时文件：覆盖同一个文件
                output_file = os.path.join(output_dir, "wallet_ranking_v2_temp.xlsx")
            else:
                # 最终文件：创建新文件
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(output_dir, f"wallet_ranking_v2_{timestamp}.xlsx")

            df.to_excel(output_file, index=False, engine='openpyxl')
            abs_path = os.path.abspath(output_file)

            # 验证文件是否真的创建成功
            if os.path.exists(output_file):
                file_size = os.path.getsize(output_file)
                logger.info(f"✅ 导出成功: {abs_path} ({len(results)} 条记录，文件大小: {file_size} 字节)")
            else:
                logger.error(f"❌ 文件保存失败: 文件不存在 {abs_path}")
                return None

            return output_file
        except Exception as e:
            logger.error(f"导出失败: {e}")
            return None


async def main():
    """主函数：批量分析入口"""
    # 检查 API Key 配置
    helius_keys = [k for k in HELIUS_KEY_LIST if k and k.strip()]
    jupiter_keys = [k for k in JUPITER_KEY_LIST if k and k.strip()]

    if not helius_keys:
        print("❌ 错误：HELIUS_KEY_LIST 未配置，请在文件开头添加你的 Helius API Keys")
        return

    if not jupiter_keys:
        print("❌ 错误：JUPITER_KEY_LIST 未配置，请在文件开头添加你的 Jupiter API Keys")
        return

    # 初始化 API Key 管理器
    helius_key_manager = APIKeyManager(helius_keys, "Helius")
    jupiter_key_manager = APIKeyManager(jupiter_keys, "Jupiter")

    logger.info(f"已配置 {len(helius_keys)} 个 Helius API Keys")
    logger.info(f"已配置 {len(jupiter_keys)} 个 Jupiter API Keys")

    # 初始化组件
    analyzer = WalletAnalyzerV2()  # 不需要传入key，因为会在调用时动态获取
    trash_manager = TrashListManager()
    batch_analyzer = BatchAnalyzerV2(
        analyzer,
        trash_manager,
        helius_key_manager,
        jupiter_key_manager,
        CONCURRENT_LIMIT
    )
    exporter = ReportExporterV2()

    # 加载钱包列表和黑名单
    trash_set = trash_manager.load()
    all_addresses = WalletListLoader.load()

    if not all_addresses:
        print("❌ 未找到钱包地址列表")
        return

    # 过滤黑名单
    addresses = [a for a in all_addresses if not trash_manager.contains(a)]
    skip_count = len(all_addresses) - len(addresses)

    if not addresses:
        print(f"🚫 库中所有地址都在黑名单内，或没有新地址。")
        return

    print(f"🚀 启动批量分析 V2 (超严格版) | 任务数: {len(addresses)} (跳过黑名单: {skip_count})")

    # 执行批量分析（每20个钱包自动保存一次）
    results = await batch_analyzer.analyze_batch(addresses, save_interval=20, exporter=exporter)

    # 导出最终结果（覆盖临时文件或创建新文件）
    if results:
        # 删除临时文件（如果存在）
        temp_file = os.path.join(RESULTS_DIR, "wallet_ranking_v2_temp.xlsx")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                logger.debug(f"已删除临时文件: {temp_file}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {e}")

        # 导出最终报告
        output_file = exporter.export(results, is_temp=False)
        if output_file:
            print(f"\n✅ 导出成功: {output_file}")
            print(f"📊 共分析 {len(results)} 个钱包，已按综合评分排序")

            # 显示前5名
            if len(results) > 0:
                print("\n🏆 Top 5 钱包:")
                for i, r in enumerate(results[:5], 1):
                    print(
                        f"  {i}. {r['钱包地址'][:8]}... | 评分: {r['综合评分']} | 评级: {r['战力评级']} | 定位: {r['最佳定位']} | 30天盈利: {r['30天盈利(SOL)']:+.2f} SOL")
        else:
            print("\n⚠️ 导出失败")
    else:
        print("\n🏁 分析结果为空，请检查报错或地址列表。")

    # 收集所有有效的钱包地址（从分析结果和原始列表中提取）
    valid_addresses = set()

    # 1. 从分析结果中提取（这些是成功分析的钱包）
    if results:
        for r in results:
            addr = r.get('钱包地址', '').strip()
            if addr and is_valid_solana_address(addr):
                valid_addresses.add(addr)

    # 2. 从原始列表中提取（包括未分析但格式正确的地址）
    for addr in all_addresses:
        addr = addr.strip()
        if addr and is_valid_solana_address(addr):
            valid_addresses.add(addr)

    # 3. 保存有效的钱包地址回文件
    if valid_addresses:
        saved = WalletListSaver.save_valid_addresses(list(valid_addresses), WALLETS_FILE)
        if saved:
            print(f"\n✅ 已过滤并保存 {len(valid_addresses)} 个有效钱包地址到 {WALLETS_FILE}")
        else:
            print(f"\n⚠️ 保存钱包地址失败")
    else:
        print(f"\n⚠️ 未找到有效的钱包地址")


if __name__ == "__main__":
    asyncio.run(main())
