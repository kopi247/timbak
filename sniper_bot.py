#!/usr/bin/env python3
"""
Solana Meme Sniper - AUTO TAKE PROFIT using Jupiter Limit Orders
No monitoring needed - Jupiter executes the sell automatically.
"""

import asyncio
import base64
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SniperBot")

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
load_dotenv()

RPC_HTTP = os.getenv("RPC_HTTP", "")
private_key_str = os.getenv("PRIVATE_KEY", "")

WALLET = Keypair.from_base58_string(private_key_str)

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
JUPITER_LIMIT_ORDER_URL = "https://jup.ag/api/limit/v1/createOrder"

SNIPE_AMOUNT_SOL = float(os.getenv("SNIPE_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "1000"))
TAKE_PROFIT_MULTIPLE = float(os.getenv("TAKE_PROFIT_MULTIPLE", "1.7"))
STOP_LOSS_FACTOR = float(os.getenv("STOP_LOSS_FACTOR", "0.6"))
GAS_RESERVE_SOL = float(os.getenv("GAS_RESERVE_SOL", "0.02"))
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "600"))
MIN_TOKEN_AGE = int(os.getenv("MIN_TOKEN_AGE", "5"))
MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", "300"))
MAX_RUGCHECK_RISK = int(os.getenv("MAX_RUGCHECK_RISK", "0"))

WSOL_MINT = "So11111111111111111111111111111111111111112"
POSITIONS_FILE = Path("positions.json")
SEEN_TOKENS: set = set()
ACTIVE_ORDERS: dict = {}
LAST_JUPITER_CALL = 0
JUPITER_RATE_LIMIT = 1.5

# ----------------------------------------------------------------------
# Rate Limiter
# ----------------------------------------------------------------------

async def rate_limit():
    global LAST_JUPITER_CALL
    elapsed = time.time() - LAST_JUPITER_CALL
    if elapsed < JUPITER_RATE_LIMIT:
        await asyncio.sleep(JUPITER_RATE_LIMIT - elapsed)
    LAST_JUPITER_CALL = time.time()

# ----------------------------------------------------------------------
# Jupiter API
# ----------------------------------------------------------------------

async def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    await rate_limit()
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "slippageBps": str(slippage_bps)}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(JUPITER_QUOTE_URL, params=params) as resp:
            if resp.status != 200:
                raise Exception(f"Quote error: {await resp.text()}")
            return await resp.json()

async def jupiter_swap(quote_response: dict, user_public_key: str) -> dict:
    await rate_limit()
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": 500000, "priorityLevel": "veryHigh"}},
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.post(JUPITER_SWAP_URL, json=payload) as resp:
            if resp.status != 200:
                raise Exception(f"Swap error: {await resp.text()}")
            return await resp.json()

# ----------------------------------------------------------------------
# Send Transaction (with high priority)
# ----------------------------------------------------------------------

async def send_transaction_urgent(swap_tx: VersionedTransaction) -> str:
    """Send with maximum priority - for sells."""
    rpc = AsyncClient(RPC_HTTP)
    raw_bytes = bytes(swap_tx)
    txid_resp = await rpc.send_raw_transaction(raw_bytes)
    sig = str(txid_resp.value) if hasattr(txid_resp, 'value') else str(txid_resp)
    logger.info(f"🚀 URGENT TX: {sig[:40]}...")
    return sig

# ----------------------------------------------------------------------
# Buy Token
# ----------------------------------------------------------------------

async def buy_token(mint: str) -> Tuple[str, float]:
    """Buy token and return (txid, token_amount_received)."""
    amount_lamports = int(SNIPE_AMOUNT_SOL * 1e9)
    quote = await jupiter_quote(WSOL_MINT, mint, amount_lamports, SLIPPAGE_BPS)
    tx_data = await jupiter_swap(quote, str(WALLET.pubkey()))
    swap_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    
    try:
        swap_tx = VersionedTransaction(swap_tx.message, [WALLET])
    except:
        pass
    
    txid = await send_transaction_urgent(swap_tx)
    
    # Get token amount from quote
    token_amount = int(quote['outAmount'])
    
    return txid, token_amount

# ----------------------------------------------------------------------
# Sell Token (instant, maximum priority)
# ----------------------------------------------------------------------

async def sell_token_instant(mint: str, token_amount: int) -> str:
    """Sell tokens IMMEDIATELY with maximum priority."""
    logger.info(f"💰 SELLING {token_amount} of {mint[:8]}...")
    
    quote = await jupiter_quote(mint, WSOL_MINT, token_amount, SLIPPAGE_BPS)
    tx_data = await jupiter_swap(quote, str(WALLET.pubkey()))
    sell_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    
    try:
        sell_tx = VersionedTransaction(sell_tx.message, [WALLET])
    except:
        pass
    
    txid = await send_transaction_urgent(sell_tx)
    
    sell_value = int(quote['outAmount']) / 1e9
    logger.info(f"✅ SOLD! TX: {txid[:40]}... Value: {sell_value:.6f} SOL")
    
    return txid

# ----------------------------------------------------------------------
# Safety Check (simplified)
# ----------------------------------------------------------------------

async def safety_check(mint: str) -> bool:
    # Age check
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("pairs"):
                        pair_created = data["pairs"][0].get("pairCreatedAt", 0)
                        if pair_created > 0:
                            age = time.time() - (pair_created / 1000)
                            if age < MIN_TOKEN_AGE or age > MAX_TOKEN_AGE:
                                return False
    except:
        pass
    
    # RugCheck
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for risk in data.get("risks", []):
                        level = risk.get("level", 0)
                        if isinstance(level, str):
                            level = int(level) if level.isdigit() else 99
                        if level > MAX_RUGCHECK_RISK:
                            return False
    except:
        pass
    
    return True

# ----------------------------------------------------------------------
# Price Monitor (check every 3 seconds, sell instantly)
# ----------------------------------------------------------------------

async def monitor_and_sell(mint: str, token_amount: int, buy_price_sol: float):
    """
    Monitor price every 3 seconds.
    If price hits target OR drops to stop-loss, sell IMMEDIATELY.
    """
    start_time = time.time()
    peak_sol = buy_price_sol
    target_sol = buy_price_sol * TAKE_PROFIT_MULTIPLE
    stop_sol = buy_price_sol * STOP_LOSS_FACTOR
    
    logger.info(f"👁️ Monitoring {mint[:8]}... Target: {target_sol:.6f} SOL | Stop: {stop_sol:.6f} SOL")
    
    while True:
        elapsed = time.time() - start_time
        
        # Time limit
        if elapsed > MAX_HOLD_SECONDS:
            logger.info(f"⏰ Time limit, selling")
            await sell_token_instant(mint, token_amount)
            return
        
        # Check price
        try:
            quote = await jupiter_quote(mint, WSOL_MINT, token_amount, SLIPPAGE_BPS)
            current_sol = int(quote['outAmount']) / 1e9
        except Exception as e:
            logger.warning(f"Quote error: {e}")
            await asyncio.sleep(3)
            continue
        
        if current_sol > peak_sol:
            peak_sol = current_sol
            logger.info(f"🔺 Peak: {peak_sol:.6f} SOL ({peak_sol/buy_price_sol:.2f}x)")
        
        cm = current_sol / buy_price_sol
        
        # TAKE PROFIT
        if current_sol >= target_sol:
            logger.info(f"💰💰💰 TARGET HIT! {cm:.2f}x! SELLING NOW!")
            await sell_token_instant(mint, token_amount)
            return
        
        # STOP LOSS
        if current_sol <= stop_sol:
            logger.info(f"🛑 STOP LOSS! {cm:.2f}x! SELLING NOW!")
            await sell_token_instant(mint, token_amount)
            return
        
        # Trailing stop if above 1.2x
        if peak_sol >= buy_price_sol * 1.2:
            drop = (1 - current_sol / peak_sol) * 100
            if drop >= 25:
                logger.info(f"📉 Trailing stop: {drop:.0f}% from peak, selling")
                await sell_token_instant(mint, token_amount)
                return
        
        logger.info(f"📊 {mint[:8]}... {cm:.2f}x | peak: {peak_sol/buy_price_sol:.2f}x")
        await asyncio.sleep(3)

# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

async def discover_new_tokens():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get("https://api.dexscreener.com/token-profiles/latest/v1") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for token in data:
                            mint = token.get("tokenAddress")
                            if token.get("chainId") == "solana" and mint not in SEEN_TOKENS:
                                SEEN_TOKENS.add(mint)
                                yield mint
            except:
                pass
            await asyncio.sleep(1)

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def main():
    logger.info("=" * 60)
    logger.info(f"⚡ SNIPER BOT - Wallet: {WALLET.pubkey()}")
    logger.info(f"Buy: {SNIPE_AMOUNT_SOL} SOL | Target: {TAKE_PROFIT_MULTIPLE}x | Stop: {(1-STOP_LOSS_FACTOR)*100:.0f}% loss")
    logger.info("=" * 60)
    
    rpc = AsyncClient(RPC_HTTP)
    balance = await rpc.get_balance(WALLET.pubkey())
    logger.info(f"Balance: {balance.value/1e9:.4f} SOL")
    logger.info("=" * 60)
    
    async for mint in discover_new_tokens():
        # Skip if already monitoring
        if mint in ACTIVE_ORDERS:
            continue
        
        # Check balance
        bal = await rpc.get_balance(WALLET.pubkey())
        if bal.value/1e9 < SNIPE_AMOUNT_SOL + GAS_RESERVE_SOL:
            logger.warning(f"Low balance: {bal.value/1e9:.4f} SOL")
            await asyncio.sleep(30)
            continue
        
        # Safety
        if not await safety_check(mint):
            continue
        
        logger.info(f"🎯 Sniping {mint[:8]}...")
        
        try:
            txid, token_amount = await buy_token(mint)
            logger.info(f"✅ Bought! {token_amount} tokens | TX: {txid[:30]}...")
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            continue
        
        # Start monitoring
        ACTIVE_ORDERS[mint] = True
        asyncio.create_task(monitor_and_sell(mint, token_amount, SNIPE_AMOUNT_SOL))

if __name__ == "__main__":
    asyncio.run(main())
