#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/solana/trader.py
@Description: Solana 交易执行模块 (本币 USDC，集成 Jito MEV 防夹 + SSL 修复版)
"""
import base64
import os
import random
import asyncio
import traceback
import base58  # 🔥 需要 pip install base58
import aiohttp
import httpx
from dotenv import load_dotenv

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.system_program import transfer, TransferParams
from spl.token.instructions import close_account, CloseAccountParams
from spl.token.constants import TOKEN_PROGRAM_ID

# 引入新配置
from config.settings import (
    PRIVATE_KEY,
    JUPITER_API_KEY,
    USE_JITO,
    JITO_TIP_AMOUNT,
    JITO_BLOCK_ENGINE_URL,
    JITO_TIP_ACCOUNTS,
    USDC_MINT,
)
from utils.logger import logger

load_dotenv()


class SolanaTrader:
    def __init__(self, rpc_endpoint):
        # 保持原有的 RPC 初始化逻辑 (配合下方的 SSL Patch)
        self.rpc_client = AsyncClient(rpc_endpoint, timeout=30)

        if not PRIVATE_KEY:
            raise ValueError("❌ 未找到私钥，请在 .env 或 config/settings.py 中配置 PRIVATE_KEY")

        try:
            if isinstance(PRIVATE_KEY, str):
                self.payer = Keypair.from_base58_string(PRIVATE_KEY)
            else:
                self.payer = Keypair.from_bytes(PRIVATE_KEY)
        except Exception as e:
            logger.error(f"私钥加载失败: {e}")
            raise e

        # 原生 SOL（用于 Gas、Jito 小费、余额检查）
        self.SOL_MINT = "So11111111111111111111111111111111111111112"
        # 本币：买卖计价与结算使用 USDC
        self.QUOTE_MINT = USDC_MINT

        # 打印配置信息
        logger.info(f"💳 交易钱包已加载: {self.payer.pubkey()}")
        logger.info(f"🔧 Jito 模式: {'✅ 已启用' if USE_JITO else '❌ 已禁用 (使用普通 RPC)'}")
        if USE_JITO:
            logger.info(f"💰 Jito 小费: {JITO_TIP_AMOUNT} SOL | 端点: {JITO_BLOCK_ENGINE_URL}")
        if JUPITER_API_KEY:
            logger.info(f"🔑 Jupiter API Key: 已配置")
        else:
            logger.warning(f"⚠️ Jupiter API Key: 未配置（可能遇到限流）")
    
    async def close(self):
        """
        关闭资源
        
        注意：在程序退出时调用此方法以正确释放资源
        """
        await self.rpc_client.close()
        logger.info("🔒 交易客户端已关闭")

    async def get_token_balance(self, wallet_address: str, token_mint: str) -> float:
        """
        获取指定代币余额
        
        Args:
            wallet_address: 钱包地址
            token_mint: 代币地址
            
        Returns:
            代币余额（浮点数），失败返回 0.0
        """
        try:
            if token_mint == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_address))
                return resp.value / 10 ** 9

            # 🔥 修复：使用 TokenAccountOpts 对象而不是字典
            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_address),
                opts
            )
            if not resp.value:
                return 0.0

            account_data = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_data)
            return float(balance_resp.value.ui_amount) if balance_resp.value.ui_amount else 0.0
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            return 0.0

    async def get_token_balance_raw(self, wallet_address: str, token_mint: str) -> int:
        """
        获取代币原始余额（返回原始整数，用于精确询价）
        
        Args:
            wallet_address: 钱包地址
            token_mint: 代币地址
            
        Returns:
            代币原始余额（整数 lamports），失败返回 None
        """
        try:
            if token_mint == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_address))
                return int(resp.value)

            # 🔥 修复：使用 TokenAccountOpts 对象而不是字典
            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_address),
                opts
            )
            if not resp.value:
                return 0

            account_data = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_data)
            return int(balance_resp.value.amount)
        except Exception as e:
            logger.warning(f"获取原始余额失败: {e}")
            return None

    async def get_quote(self, session, input_mint, output_mint, amount_lamports, slippage_bps=50):
        """
        从 Jupiter 获取报价
        
        Args:
            session: aiohttp 会话
            input_mint: 输入代币地址
            output_mint: 输出代币地址
            amount_lamports: 输入数量（lamports，整数）
            slippage_bps: 滑点（basis points）
            
        Returns:
            quote 响应数据，失败返回 None
        """
        # 🔥 修复：使用正确的 Jupiter API v1 端点
        url = "https://api.jup.ag/swap/v1/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_lamports)),  # Jupiter API 需要字符串格式
            "slippageBps": slippage_bps
        }
        # 🔥 修复：添加 Jupiter API Key 支持
        headers = {"Accept": "application/json"}
        if JUPITER_API_KEY:
            headers["x-api-key"] = JUPITER_API_KEY
        
        try:
            # 🔥 添加超时设置，防止长时间等待
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            async with session.get(url, params=params, headers=headers, timeout=timeout) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ 询价API失败 [{response.status}]: {error_text[:500]}")
                    return None
                return await response.json()
        except asyncio.TimeoutError:
            logger.error(f"❌ 询价超时: Jupiter API 响应超时（30秒）")
            return None
        except Exception as e:
            logger.error(f"❌ 询价网络异常: {e}")
            return None

    async def _send_via_rpc(self, swap_transaction_buf, est_out):
        """
        通过普通 RPC 发送交易（降级方法）
        
        Args:
            swap_transaction_buf: Swap 交易字节数据
            est_out: 预计输出数量
            
        Returns:
            (success: bool, out_amount: int): 交易是否成功，预计输出数量
        """
        try:
            logger.info("📡 [降级模式] 使用普通 RPC 发送交易...")
            tx = VersionedTransaction.from_bytes(swap_transaction_buf)
            signed_tx = VersionedTransaction(tx.message, [self.payer])

            opts = TxOpts(skip_preflight=True, max_retries=3)
            signature = await self.rpc_client.send_transaction(signed_tx, opts=opts)
            logger.info(f"✅ [降级模式] 普通交易发送成功! Hash: {signature.value}")

            await asyncio.sleep(2)
            return True, est_out
        except Exception as e:
            logger.error(f"❌ [降级模式] 普通交易执行异常: {e}")
            logger.error(traceback.format_exc())
            return False, 0

    async def send_jito_bundle(self, jupiter_tx_bytes):
        """
        🚀 [新增] 发送 Jito Bundle (Jupiter Swap + 小费)
        
        Returns:
            (success: bool, swap_signature: str): 是否成功，Swap 交易签名（用于后续查询）
        """
        try:
            # 1. 解析 Jupiter 返回的交易
            swap_tx = VersionedTransaction.from_bytes(jupiter_tx_bytes)

            # 2. 构建小费交易 (Tip Transaction)
            tip_account = random.choice(JITO_TIP_ACCOUNTS)
            tip_lamports = int(JITO_TIP_AMOUNT * 10 ** 9)

            latest_blockhash = await self.rpc_client.get_latest_blockhash()
            blockhash = latest_blockhash.value.blockhash

            tip_ix = transfer(
                TransferParams(
                    from_pubkey=self.payer.pubkey(),
                    to_pubkey=Pubkey.from_string(tip_account),
                    lamports=tip_lamports
                )
            )

            tip_msg = MessageV0.try_compile(
                self.payer.pubkey(),
                [tip_ix],
                [],
                blockhash
            )
            tip_tx = VersionedTransaction(tip_msg, [self.payer])

            # 3. 重新签署 Swap 交易
            # 🔥 注意：Jupiter 返回的交易可能已经部分签名，但我们仍需要用自己的私钥签名
            # 使用 swap_tx.message 重新构建交易，确保使用最新的 blockhash
            signed_swap_tx = VersionedTransaction(swap_tx.message, [self.payer])
            
            # 🔥 提取 Swap 交易签名（用于后续查询状态）
            # 注意：VersionedTransaction 的签名在创建时已经生成，可以直接获取
            if signed_swap_tx.signatures and len(signed_swap_tx.signatures) > 0:
                swap_signature = signed_swap_tx.signatures[0]
                swap_sig_str = str(swap_signature)
            else:
                # 如果签名不存在，尝试从消息计算（备用方案）
                logger.warning("⚠️ 无法从交易中提取签名，将无法查询交易状态")
                swap_sig_str = None

            # 4. 编码为 Base58 (Jito API 要求)
            b58_swap = base58.b58encode(bytes(signed_swap_tx)).decode('utf-8')
            b58_tip = base58.b58encode(bytes(tip_tx)).decode('utf-8')

            # 5. 发送 Bundle
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [[b58_swap, b58_tip]]
            }

            logger.info(f"🚀 发送 Jito Bundle... (节点: {JITO_BLOCK_ENGINE_URL}, 小费: {JITO_TIP_AMOUNT} SOL)")

            async with aiohttp.ClientSession() as session:
                async with session.post(JITO_BLOCK_ENGINE_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"❌ Jito API 请求失败 [{resp.status}]: {error_text[:500]}")
                        return False, None
                    
                    data = await resp.json()
                    if "result" in data:
                        bundle_id = data["result"]
                        logger.info(f"✅ Jito Bundle 已提交! Bundle ID: {bundle_id}")
                        if swap_sig_str:
                            logger.info(f"📝 Swap 交易签名: {swap_sig_str}")
                            logger.info(f"🔗 查看交易: https://solscan.io/tx/{swap_sig_str}")
                        return True, swap_sig_str
                    elif "error" in data:
                        logger.error(f"❌ Jito 发送失败: {data.get('error', {})}")
                        return False, None
                    else:
                        logger.error(f"❌ Jito 响应格式异常: {data}")
                        return False, None

        except Exception as e:
            logger.error(f"💥 Jito Bundle 构建异常: {e}")
            logger.error(traceback.format_exc())
            return False, None
    
    async def check_transaction_status(self, signature_str, max_retries=10, wait_seconds=3):
        """
        检查交易状态
        
        Args:
            signature_str: 交易签名（字符串），如果为 None 则跳过检查
            max_retries: 最大重试次数
            wait_seconds: 每次重试等待时间（秒）
            
        Returns:
            bool: 交易是否已确认（如果 signature_str 为 None，返回 True）
        """
        if not signature_str:
            logger.warning("⚠️ 无法检查交易状态（签名不可用）")
            return True  # 返回 True，让后续的余额检查来确认
        
        from solders.signature import Signature
        
        try:
            sig = Signature.from_string(signature_str)
            
            for i in range(max_retries):
                try:
                    # 查询交易状态
                    resp = await self.rpc_client.get_signature_statuses([sig])
                    if resp.value and resp.value[0]:
                        status = resp.value[0]
                        if status.confirmation_status:
                            logger.info(f"✅ 交易已确认! 状态: {status.confirmation_status}")
                            return True
                        elif status.err:
                            logger.error(f"❌ 交易失败: {status.err}")
                            return False
                    
                    # 如果还没确认，等待后重试
                    if i < max_retries - 1:
                        logger.info(f"⏳ 交易未确认，等待 {wait_seconds} 秒后重试 ({i+1}/{max_retries})...")
                        await asyncio.sleep(wait_seconds)
                    
                except Exception as e:
                    logger.warning(f"⚠️ 查询交易状态异常: {e}")
                    if i < max_retries - 1:
                        await asyncio.sleep(wait_seconds)
            
            logger.warning(f"⚠️ 交易状态查询超时（{max_retries} 次重试），但可能仍在处理中")
            return False  # 返回 False，但后续仍会检查余额
            
        except Exception as e:
            logger.error(f"❌ 检查交易状态异常: {e}")
            return False

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=50):
        """
        执行 Swap 交易 (修改版：支持 Jito / 普通 RPC 切换)
        """
        async with aiohttp.ClientSession() as session:
            # 1. 询价
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote:
                return False, 0

            est_out = int(quote.get("outAmount", 0))

            # 2. 获取交易数据
            # 如果开启 Jito，不需要 Jupiter 加优先费(auto)，因为我们会自己付小费
            # 如果关闭 Jito，还是加上 auto 比较稳
            priority_fee = "auto" if not USE_JITO else 0

            swap_payload = {
                "quoteResponse": quote,
                "userPublicKey": str(self.payer.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": priority_fee
            }

            # 🔥 修复：使用正确的 Jupiter Swap API v1 端点
            swap_url = "https://api.jup.ag/swap/v1/swap"
            # 🔥 修复：添加 Jupiter API Key 支持
            headers = {"Content-Type": "application/json"}
            if JUPITER_API_KEY:
                headers["x-api-key"] = JUPITER_API_KEY
            
            try:
                async with session.post(swap_url, json=swap_payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ Swap API失败 [{response.status}]: {error_text[:500]}")
                        return False, 0
                    swap_resp = await response.json()
            except Exception as e:
                logger.error(f"❌ Jupiter API 请求失败: {e}")
                logger.error(traceback.format_exc())
                return False, 0

            if "swapTransaction" not in swap_resp:
                logger.error(f"❌ 获取 Swap 交易失败: {swap_resp}")
                return False, 0

            swap_transaction_buf = base64.b64decode(swap_resp["swapTransaction"])

            # --- 分支逻辑：Jito vs 普通 RPC ---
            if USE_JITO:
                # 🅰️ Jito 模式（带降级机制）
                success, swap_signature = await self.send_jito_bundle(swap_transaction_buf)
                if success and swap_signature:
                    # 🔥 新增：检查交易状态（最多等待 30 秒）
                    logger.info("⏳ 等待 Jito Bundle 上链确认...")
                    confirmed = await self.check_transaction_status(swap_signature, max_retries=10, wait_seconds=3)
                    if confirmed:
                        logger.info("✅ Jito Bundle 交易已确认上链！")
                        return True, est_out
                    else:
                        logger.warning("⚠️ Jito Bundle 交易未在预期时间内确认，但可能仍在处理中...")
                        # 即使未确认，也返回成功，让后续的余额检查来最终确认
                        return True, est_out
                else:
                    # 🔥 降级机制：Jito 失败时自动降级到普通 RPC
                    logger.warning("⚠️ Jito 发送失败，自动降级到普通 RPC 模式...")
                    return await self._send_via_rpc(swap_transaction_buf, est_out)
            else:
                # 🅱️ 普通 RPC 模式（直接使用）
                return await self._send_via_rpc(swap_transaction_buf, est_out)

    async def close_token_account(self, token_mint_str):
        """ 🔥 回收租金：关闭空的代币账户，拿回 0.002 SOL """
        try:
            # 1. 查找该代币的 ATA (关联账户)
            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(self.payer.pubkey(), opts)

            if not resp.value:
                logger.info(f"⚠️ 账户不存在，无需关闭: {token_mint_str}")
                return False

            token_account_pubkey = resp.value[0].pubkey

            # 2. 构建关闭指令 (CloseAccount)
            close_ix = close_account(
                CloseAccountParams(
                    account=token_account_pubkey,
                    dest=self.payer.pubkey(),
                    owner=self.payer.pubkey(),
                    program_id=TOKEN_PROGRAM_ID
                )
            )

            # 3. 构建并发送交易 (Versioned Transaction)
            # 获取最新的 blockhash
            latest_blockhash = await self.rpc_client.get_latest_blockhash()
            msg = MessageV0.try_compile(
                self.payer.pubkey(),
                [close_ix],
                [],
                latest_blockhash.value.blockhash,
            )
            vtx = VersionedTransaction(msg, [self.payer])

            opts = TxOpts(skip_preflight=True)
            await self.rpc_client.send_transaction(vtx, opts=opts)

            logger.info(f"♻️ [房租回收] 成功关闭账户，回血 +0.002 SOL")
            return True

        except Exception as e:
            logger.warning(f"⚠️ 关闭账户失败 (可能由粉尘残留导致): {e}")
            return False


# 🔥 Monkey Patch: 强制修改 httpx 的默认行为，使其不验证 SSL
# 这一步是为了解决 Solana RPC (httpx) 在代理下的报错问题
def patch_httpx_verify():
    original_init = httpx.AsyncClient.__init__

    def new_init(self, *args, **kwargs):
        kwargs['verify'] = False  # 强制关闭验证
        original_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = new_init


patch_httpx_verify()
