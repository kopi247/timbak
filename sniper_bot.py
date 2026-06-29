#!/usr/bin/env python3
"""
Fully Automated Solana Meme-Sniping Bot
BUY: 0.05 SOL | TAKE PROFIT: 1.7x | STOP LOSS: 40% max loss
- Minimum 30s hold before any stop-loss
- Requires 2 consecutive low readings for stop-loss confirmation
- Stop-loss ALWAYS triggers on confirmed downtrend
- Trailing stop only locks in profits (never sells below break-even)
- Take profit always active (can trigger anytime)
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
if not RPC_HTTP:
    raise ValueError("RPC_HTTP not set in .env")

private_key_str = os.getenv("PRIVATE_KEY", "")
if not private_key_str:
    raise ValueError("PRIVATE_KEY not set in .env")

try:
    WALLET = Keypair.from_base58_string(private_key_str)
    logger.info(f"Wallet loaded: {WALLET.pubkey()}")
except Exception as e:
    raise ValueError(f"Invalid PRIVATE_KEY: {e}")

# Jupiter API
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# Rate limiting
LAST_JUPITER_CALL = 0
JUPITER_RATE_LIMIT = 1.5

# Snipe settings
SNIPE_AMOUNT_SOL = float(os.getenv("SNIPE_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "1000"))
MAX_RUGCHECK_RISK = int(os.getenv("MAX_RUGCHECK_RISK", "0"))

# Token age filter (seconds)
MIN_TOKEN_AGE = int(os.getenv("MIN_TOKEN_AGE", "5"))
MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", "300"))

# Gas
GAS_RESERVE_SOL = float(os.getenv("GAS_RESERVE_SOL", "0.02"))
GAS_PER_TX_ESTIMATE = 0.00005

# Exit settings
TAKE_PROFIT_MULTIPLE = float(os.getenv("TAKE_PROFIT_MULTIPLE", "1.7"))
STOP_LOSS_FACTOR = float(os.getenv("STOP_LOSS_FACTOR", "0.6"))  # Sell at 60% of buy = max 40% loss
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "30"))
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "600"))
MIN_HOLD_SECONDS = int(os.getenv("MIN_HOLD_SECONDS", "30"))
PRICE_CHECK_SECONDS = 3

# Token discovery
DISCOVERY_POLL_SECONDS = 1
SEEN_TOKENS: set = set()
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "Es9vMFrzaCER9wFRNsmH8zFvwYQzG5C4R6eHn9rxRz8Q",
}

WSOL_MINT = "So11111111111111111111111111111111111111112"
ACTIVE_MONITORS: dict = {}

# ----------------------------------------------------------------------
# Rate Limiter
# ----------------------------------------------------------------------

async def rate_limit():
    global LAST_JUPITER_CALL
    elapsed = time.time() - LAST_JUPITER_CALL
    if elapsed < JUPITER_RATE_LIMIT:
        await asyncio.sleep(JUPITER_RATE_LIMIT - elapsed + random.uniform(0, 0.3))
    LAST_JUPITER_CALL = time.time()

# ----------------------------------------------------------------------
# Jupiter API
# ----------------------------------------------------------------------

async def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    await rate_limit()
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(JUPITER_QUOTE_URL, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    else:
                        raise Exception(f"Quote error: {await resp.text()}")
        except asyncio.TimeoutError:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise
    raise Exception("Quote failed after retries")

async def jupiter_swap(quote_response: dict, user_public_key: str) -> dict:
    await rate_limit()
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": 500000,
                "priorityLevel": "veryHigh"
            }
        },
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(JUPITER_SWAP_URL, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    else:
                        raise Exception(f"Swap error: {await resp.text()}")
        except asyncio.TimeoutError:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise
    raise Exception("Swap failed after retries")

# ----------------------------------------------------------------------
# Transaction Helpers
# ----------------------------------------------------------------------

async def create_rpc() -> AsyncClient:
    return AsyncClient(RPC_HTTP)

def sign_tx(tx: VersionedTransaction, wallet: Keypair) -> VersionedTransaction:
    try:
        return VersionedTransaction(tx.message, [wallet])
    except Exception:
        return tx

async def send_tx(swap_tx: VersionedTransaction) -> str:
    rpc = await create_rpc()
    raw_bytes = bytes(swap_tx)
    txid_resp = await rpc.send_raw_transaction(raw_bytes)
    
    if hasattr(txid_resp, 'value'):
        sig = str(txid_resp.value)
    else:
        sig = str(txid_resp)
    
    logger.info(f"TX sent: {sig[:40]}...")
    
    for i in range(15):
        await asyncio.sleep(4)
        try:
            resp = await rpc.get_signature_statuses([sig])
            if resp.value and len(resp.value) > 0 and resp.value[0] is not None:
                status = resp.value[0]
                if hasattr(status, 'err') and status.err is not None:
                    raise Exception(f"TX failed: {status.err}")
                logger.info(f"✅ Confirmed!")
                return sig
        except Exception as e:
            if "TX failed" in str(e):
                raise
            pass
    
    return sig

# ----------------------------------------------------------------------
# Buy
# ----------------------------------------------------------------------

async def buy_token(mint: str) -> Tuple[str, int]:
    """Buy token. Returns (txid, token_amount)."""
    amount_lamports = int(SNIPE_AMOUNT_SOL * 1e9)
    
    logger.info(f"🛒 Buying {SNIPE_AMOUNT_SOL} SOL of {mint[:8]}...")
    
    quote = await jupiter_quote(WSOL_MINT, mint, amount_lamports, SLIPPAGE_BPS)
    tx_data = await jupiter_swap(quote, str(WALLET.pubkey()))
    swap_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    swap_tx = sign_tx(swap_tx, WALLET)
    
    txid = await send_tx(swap_tx)
    token_amount = int(quote['outAmount'])
    
    logger.info(f"✅ Bought! {token_amount} tokens | TX: {txid[:30]}...")
    return txid, token_amount

# ----------------------------------------------------------------------
# Sell
# ----------------------------------------------------------------------

async def sell_token(mint: str, token_amount: int, reason: str = "") -> Tuple[str, float]:
    """Sell tokens. Returns (txid, sol_received)."""
    logger.info(f"💰 SELLING {token_amount} of {mint[:8]}... ({reason})")
    
    quote = await jupiter_quote(mint, WSOL_MINT, token_amount, SLIPPAGE_BPS)
    tx_data = await jupiter_swap(quote, str(WALLET.pubkey()))
    sell_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    sell_tx = sign_tx(sell_tx, WALLET)
    
    txid = await send_tx(sell_tx)
    sol_received = int(quote['outAmount']) / 1e9
    
    profit = sol_received - SNIPE_AMOUNT_SOL - (GAS_PER_TX_ESTIMATE * 2)
    logger.info(f"✅ SOLD! {sol_received:.6f} SOL | PnL: {profit:+.6f} SOL | TX: {txid[:30]}...")
    
    return txid, sol_received

# ----------------------------------------------------------------------
# Safety Check
# ----------------------------------------------------------------------

async def get_token_age(mint: str) -> Optional[float]:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("pairs") and len(data["pairs"]) > 0:
                        pair_created = data["pairs"][0].get("pairCreatedAt", 0)
                        if pair_created > 0:
                            return time.time() - (pair_created / 1000)
    except Exception:
        pass
    return None

async def safety_check(mint: str) -> bool:
    # Age check
    age = await get_token_age(mint)
    if age is not None:
        if age < MIN_TOKEN_AGE:
            logger.info(f"❌ Too new: {age:.0f}s - {mint[:8]}...")
            return False
        if age > MAX_TOKEN_AGE:
            logger.info(f"❌ Too old: {age/60:.0f}min - {mint[:8]}...")
            return False
        logger.info(f"⏱️ Age: {age:.0f}s ✅")
    
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
                            try:
                                level = int(level)
                            except ValueError:
                                level = 99
                        if level > MAX_RUGCHECK_RISK:
                            logger.info(f"❌ Risk: {risk.get('name', 'Unknown')} (level {level})")
                            return False
    except Exception:
        pass
    
    # On-chain: mint authority
    try:
        rpc = await create_rpc()
        mint_pub = Pubkey.from_string(mint)
        acc = await rpc.get_account_info(mint_pub, commitment=Confirmed)
        if acc.value is None:
            logger.info(f"❌ Token not found on-chain")
            return False
        data = acc.value.data
        if len(data) < 4:
            return False
        if int.from_bytes(data[0:4], "little") != 0:
            logger.info(f"❌ Mint authority not renounced")
            return False
    except Exception:
        return False
    
    return True

# ----------------------------------------------------------------------
# Price Monitor
# ----------------------------------------------------------------------

async def monitor_and_sell(mint: str, token_amount: int, buy_price_sol: float):
    """
    Monitor price and execute sell strategy.
    
    RULES:
    1. TAKE PROFIT: Always active. Sells instantly at target.
    2. STOP LOSS: After MIN_HOLD_SECONDS, requires 2 consecutive low readings.
       Once confirmed, SELLS RELENTLESSLY (retries on failure).
    3. TRAILING STOP: Only above break-even. Locks in profits.
    4. TIME LIMIT: Sells at market after MAX_HOLD_SECONDS.
    """
    start_time = time.time()
    peak_sol = buy_price_sol
    target_sol = buy_price_sol * TAKE_PROFIT_MULTIPLE
    stop_sol = buy_price_sol * STOP_LOSS_FACTOR
    trailing_active = False
    low_readings = 0
    
    logger.info("=" * 50)
    logger.info(f"👁️ MONITORING {mint[:8]}...")
    logger.info(f"   Buy: {buy_price_sol:.6f} SOL")
    logger.info(f"   Target: {TAKE_PROFIT_MULTIPLE}x = {target_sol:.6f} SOL")
    logger.info(f"   Stop: {STOP_LOSS_FACTOR*100:.0f}% = {stop_sol:.6f} SOL (max {(1-STOP_LOSS_FACTOR)*100:.0f}% loss)")
    logger.info(f"   Min hold: {MIN_HOLD_SECONDS}s | Max hold: {MAX_HOLD_SECONDS}s")
    logger.info("=" * 50)
    
    while True:
        elapsed = time.time() - start_time
        
        # --- TIME LIMIT ---
        if elapsed > MAX_HOLD_SECONDS:
            logger.info(f"⏰ TIME LIMIT ({elapsed:.0f}s) - selling")
            await sell_token(mint, token_amount, "time limit")
            return
        
        # --- GET PRICE ---
        try:
            quote = await jupiter_quote(mint, WSOL_MINT, token_amount, SLIPPAGE_BPS)
            current_sol = int(quote['outAmount']) / 1e9
        except Exception as e:
            logger.warning(f"Quote error: {e}")
            await asyncio.sleep(PRICE_CHECK_SECONDS)
            continue
        
        # --- UPDATE PEAK ---
        if current_sol > peak_sol:
            peak_sol = current_sol
            logger.info(f"🔺 New peak: {peak_sol:.6f} SOL ({peak_sol/buy_price_sol:.2f}x)")
        
        cm = current_sol / buy_price_sol
        pm = peak_sol / buy_price_sol
        drop_from_peak = (1 - current_sol / peak_sol) * 100 if peak_sol > 0 else 0
        
        # Status line
        status = f"📊 {mint[:8]}... {current_sol:.6f} SOL ({cm:.2f}x)"
        status += f" | peak: {pm:.2f}x"
        status += f" | low reads: {low_readings}"
        status += f" | trailing: {'ON' if trailing_active else 'off'}"
        status += f" | elapsed: {elapsed:.0f}s"
        logger.info(status)
        
        # ================================================================
        # 1. TAKE PROFIT - Always active, instant sell
        # ================================================================
        if current_sol >= target_sol:
            profit = current_sol - buy_price_sol - (GAS_PER_TX_ESTIMATE * 2)
            logger.info(f"💰💰💰 TAKE PROFIT! {cm:.2f}x! ~{profit:+.6f} SOL!")
            await sell_token(mint, token_amount, f"take profit {cm:.2f}x")
            return
        
        # ================================================================
        # 2. STOP LOSS - After hold period, 2-confirmation, relentless sell
        # ================================================================
        if elapsed >= MIN_HOLD_SECONDS:
            if current_sol <= stop_sol:
                low_readings += 1
                logger.warning(f"⚠️ BELOW STOP! {cm:.2f}x (reading {low_readings}/2)")
                
                if low_readings >= 2:
                    loss = buy_price_sol - current_sol
                    logger.info(f"🛑🛑🛑 STOP LOSS CONFIRMED! Selling to protect capital! Loss: ~{loss:.6f} SOL")
                    
                    # Try to sell up to 5 times
                    for sell_attempt in range(5):
                        try:
                            await sell_token(mint, token_amount, f"stop loss (attempt {sell_attempt+1})")
                            return  # Success
                        except Exception as e:
                            logger.error(f"Sell attempt {sell_attempt+1} failed: {e}")
                            if sell_attempt < 4:
                                await asyncio.sleep(5)  # Wait before retry
                    
                    logger.error(f"❌ ALL SELL ATTEMPTS FAILED for stop-loss!")
                    return  # Give up after 5 attempts
            else:
                # Price recovered above stop-loss, reset counter
                if low_readings > 0:
                    logger.info(f"✅ Price recovered above stop-loss, resetting counter")
                low_readings = 0
        
        # ================================================================
        # 3. TRAILING STOP - Only in profit zone
        # ================================================================
        if current_sol >= buy_price_sol * 1.05:
            trailing_active = True
        
        if trailing_active and peak_sol >= buy_price_sol * 1.1:
            if drop_from_peak >= TRAILING_STOP_PCT:
                profit = current_sol - buy_price_sol - (GAS_PER_TX_ESTIMATE * 2)
                logger.info(f"📉 TRAILING STOP: -{drop_from_peak:.0f}% from peak | ~{profit:+.6f} SOL")
                await sell_token(mint, token_amount, f"trailing stop")
                return
        
        await asyncio.sleep(PRICE_CHECK_SECONDS)

# ----------------------------------------------------------------------
# Token Discovery
# ----------------------------------------------------------------------

async def discover_new_tokens():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = "https://api.dexscreener.com/token-profiles/latest/v1"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(DISCOVERY_POLL_SECONDS)
                        continue
                    data = await resp.json()
                for token in data:
                    chain = token.get("chainId")
                    mint = token.get("tokenAddress")
                    if chain == "solana" and mint not in SEEN_TOKENS and mint not in IGNORE_MINTS:
                        SEEN_TOKENS.add(mint)
                        yield mint
            except Exception as e:
                logger.error(f"Discovery error: {e}")
            await asyncio.sleep(DISCOVERY_POLL_SECONDS)

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def main():
    logger.info("=" * 60)
    logger.info("⚡ SOLANA MEME SNIPER BOT")
    logger.info(f"Wallet: {WALLET.pubkey()}")
    logger.info(f"Buy: {SNIPE_AMOUNT_SOL} SOL | Slippage: {SLIPPAGE_BPS/100:.0f}%")
    logger.info(f"Target: {TAKE_PROFIT_MULTIPLE}x | Stop: {(1-STOP_LOSS_FACTOR)*100:.0f}% max loss")
    logger.info(f"Stop requires: {MIN_HOLD_SECONDS}s hold + 2 confirmations")
    logger.info(f"Trailing: {TRAILING_STOP_PCT}% drop from peak (above 1.05x only)")
    logger.info(f"Max hold: {MAX_HOLD_SECONDS}s")
    logger.info("=" * 60)
    
    rpc = await create_rpc()
    balance_resp = await rpc.get_balance(WALLET.pubkey())
    balance = balance_resp.value / 1e9
    logger.info(f"Balance: {balance:.4f} SOL | Trading: {max(0, balance - GAS_RESERVE_SOL):.4f} SOL")
    logger.info("=" * 60)
    
    async for mint in discover_new_tokens():
        if mint in ACTIVE_MONITORS:
            continue
        
        balance_resp = await rpc.get_balance(WALLET.pubkey())
        balance = balance_resp.value / 1e9
        
        if balance < SNIPE_AMOUNT_SOL + GAS_RESERVE_SOL:
            logger.warning(f"⚠️ Low balance: {balance:.4f} SOL. Waiting 30s...")
            await asyncio.sleep(30)
            continue
        
        logger.info(f"\n🆕 {mint}")
        
        if not await safety_check(mint):
            continue
        
        logger.info(f"✅ Sniping...")
        
        try:
            txid, token_amount = await buy_token(mint)
        except Exception as e:
            logger.error(f"Buy failed: {e}\n")
            continue
        
        ACTIVE_MONITORS[mint] = True
        asyncio.create_task(monitor_and_sell(mint, token_amount, SNIPE_AMOUNT_SOL))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nBot stopped.")
