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
from typing import Dict, List, Optional, Set

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm

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
        concurrent_limit: int = CONCURRENT_LIMIT
    ):
        """
        初始化批量分析器
        
        Args:
            analyzer: 钱包分析器实例
            trash_manager: 黑名单管理器实例
            concurrent_limit: 数据处理并发限制（API调用始终串行）
        """
        self.analyzer = analyzer
        self.trash_manager = trash_manager
        self.concurrent_limit = concurrent_limit
        # 数据处理并发控制
        self.data_processing_semaphore = asyncio.Semaphore(concurrent_limit)
        # API调用串行化锁（确保同一时间只有一个API调用）
        self.api_lock = asyncio.Semaphore(1)
    
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
            # === 阶段1：API调用（串行化）===
            # 使用 API 锁确保同一时间只有一个 API 调用
            async with self.api_lock:
                # 1. 拉取交易数据（Helius API）
                txs = await self.analyzer.fetch_history_pagination(session, address, max_txs)
                if not txs:
                    pbar.update(1)
                    return None
                
                # 2. 解析代币项目（内部会调用 Jupiter API）
                analysis_result = await self.analyzer.parse_token_projects(session, txs, address)
                if not analysis_result.get("results"):
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
                win_rate = len(wins) / len(results) if results else 0
                total_profit = profit_dim.get("total_profit", 0)
                max_roi = profit_dim.get("max_roi", 0)
                
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
                    "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "🛡️ 稳健中军": positioning.get("🛡️ 稳健中军", 0),
                    "⚔️ 土狗猎手": positioning.get("⚔️ 土狗猎手", 0),
                    "💎 钻石之手": positioning.get("💎 钻石之手", 0),
                    "🚀 短线高手": positioning.get("🚀 短线高手", 0),
                }
            
        except Exception as e:
            logger.error(f"分析钱包 {address[:6]}... 时出错: {e}")
            pbar.update(1)
            return None
    
    async def analyze_batch(
        self,
        addresses: List[str],
        max_txs: int = 5000
    ) -> List[Dict]:
        """
        批量分析钱包列表（生产者-消费者模式）
        
        设计：
        - 所有任务并发创建（生产者）
        - API调用串行化（通过api_lock）
        - 数据处理并发（通过data_processing_semaphore）
        
        Args:
            addresses: 钱包地址列表
            max_txs: 每个钱包最大交易数量
            
        Returns:
            分析结果列表
        """
        pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")
        
        async def analyze_task(session, addr):
            """
            单个钱包分析任务（生产者）
            内部会通过锁控制API调用串行，数据处理并发
            """
            return await self.analyze_one_wallet(session, addr, pbar, max_txs)
        
        # 创建所有任务并发执行（生产者模式）
        # API调用会在内部通过api_lock串行化
        # 数据处理可以通过data_processing_semaphore并发
        async with aiohttp.ClientSession() as session:
            tasks = [analyze_task(session, addr) for addr in addresses]
            raw_results = await asyncio.gather(*tasks)
            results = [r for r in raw_results if r is not None]
        
        pbar.close()
        return results


class ReportExporterV2:
    """
    报告导出器 V2：负责导出分析结果到 Excel（包含详细评分）
    """
    
    @staticmethod
    def export(results: List[Dict], output_dir: str = RESULTS_DIR) -> Optional[str]:
        """
        导出分析结果到 Excel
        
        Args:
            results: 分析结果列表
            output_dir: 输出目录
            
        Returns:
            输出文件路径，如果失败则返回 None
        """
        if not results:
            logger.warning("没有结果可导出")
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
                "项目总数", "🛡️ 稳健中军", "⚔️ 土狗猎手", "💎 钻石之手", "🚀 短线高手",
                "分析时间"
            ]
            
            # 确保所有列都存在
            available_cols = [col for col in important_cols if col in df.columns]
            remaining_cols = [col for col in df.columns if col not in available_cols]
            df = df[available_cols + remaining_cols]
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = os.path.join(output_dir, f"wallet_ranking_v2_{timestamp}.xlsx")
            df.to_excel(output_file, index=False, engine='openpyxl')
            logger.info(f"导出成功: {output_file} ({len(results)} 条记录)")
            return output_file
        except Exception as e:
            logger.error(f"导出失败: {e}")
            return None


async def main():
    """主函数：批量分析入口"""
    # 初始化组件
    analyzer = WalletAnalyzerV2()
    trash_manager = TrashListManager()
    batch_analyzer = BatchAnalyzerV2(analyzer, trash_manager, CONCURRENT_LIMIT)
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
    
    # 执行批量分析
    results = await batch_analyzer.analyze_batch(addresses)
    
    # 导出结果
    if results:
        output_file = exporter.export(results)
        if output_file:
            print(f"\n✅ 导出成功: {output_file}")
            print(f"📊 共分析 {len(results)} 个钱包，已按综合评分排序")
            
            # 显示前5名
            if len(results) > 0:
                print("\n🏆 Top 5 钱包:")
                for i, r in enumerate(results[:5], 1):
                    print(f"  {i}. {r['钱包地址'][:8]}... | 评分: {r['综合评分']} | 评级: {r['战力评级']} | 定位: {r['最佳定位']} | 30天盈利: {r['30天盈利(SOL)']:+.2f} SOL")
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
