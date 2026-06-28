#!/usr/bin/env python3
"""
Fully Automated Solana Meme-Sniping Bot
Ultra-fast exit: 1-second price checks, instant sell on target.
Backup DexScreener watcher for missed spikes.
0.05 SOL buys, 10% slippage, RPC only (no Jito tips).
"""

import asyncio
import base64
import json
import logging
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.message import MessageV0

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

# Solana RPC
RPC_HTTP = os.getenv("RPC_HTTP", "")
if not RPC_HTTP:
    raise ValueError("RPC_HTTP not set in .env")

# Wallet
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
JUPITER_RATE_LIMIT = 1.2  # Slightly faster

# Snipe settings
SNIPE_AMOUNT_SOL = float(os.getenv("SNIPE_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "1000"))
MAX_RUGCHECK_RISK = int(os.getenv("MAX_RUGCHECK_RISK", "0"))

# Token age filter
MIN_TOKEN_AGE = int(os.getenv("MIN_TOKEN_AGE", "5"))
MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", "300"))

# Gas
GAS_RESERVE_SOL = float(os.getenv("GAS_RESERVE_SOL", "0.02"))
GAS_PER_TX_ESTIMATE = 0.00005

# Exit settings
TAKE_PROFIT_MULTIPLE = float(os.getenv("TAKE_PROFIT_MULTIPLE", "1.7"))
TRAILING_STOP_PCT = 25.0
STOP_LOSS_FACTOR = 0.6
MAX_HOLD_SECONDS = 600
FORCE_SELL_TIMEOUT = 300
PRICE_CHECK_INTERVAL = 1  # Check every 1 second

# Token discovery
DISCOVERY_POLL_SECONDS = 1
SEEN_TOKENS: set = set()
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "Es9vMFrzaCER9wFRNsmH8zFvwYQzG5C4R6eHn9rxRz8Q",
}

WSOL_MINT = "So11111111111111111111111111111111111111112"
POSITIONS_FILE = Path("positions.json")
ACTIVE_MONITORS: dict = {}

# SOL price for USD conversion (rough estimate)
SOL_PRICE_USD = 140.0

# ----------------------------------------------------------------------
# Position Persistence
# ----------------------------------------------------------------------

def save_position(mint: str, token_amount: int, buy_price_sol: float):
    positions = load_positions()
    positions[mint] = {
        "token_amount": token_amount,
        "buy_price_sol": buy_price_sol,
        "timestamp": time.time(),
    }
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)
    logger.info(f"💾 Position saved: {mint[:8]}... ({token_amount} tokens)")

def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def remove_position(mint: str):
    positions = load_positions()
    if mint in positions:
        del positions[mint]
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        logger.info(f"💾 Position removed: {mint[:8]}...")

# ----------------------------------------------------------------------
# Wallet Balance
# ----------------------------------------------------------------------

async def get_wallet_balance(rpc: AsyncClient) -> float:
    try:
        resp = await rpc.get_balance(WALLET.pubkey(), commitment=Confirmed)
        return resp.value / 1e9
    except Exception as e:
        logger.error(f"Balance check failed: {e}")
        return 0.0

def can_afford_buy(wallet_balance_sol: float) -> bool:
    total_needed = SNIPE_AMOUNT_SOL + GAS_PER_TX_ESTIMATE
    return wallet_balance_sol >= (total_needed + GAS_RESERVE_SOL)

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
                "maxLamports": 100000,
                "priorityLevel": "medium"
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
# Helpers
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

async def get_token_balance(rpc: AsyncClient, wallet: Pubkey, mint: Pubkey) -> int:
    try:
        from spl.token.instructions import get_associated_token_address
        ata = get_associated_token_address(wallet, mint)
        for attempt in range(8):
            try:
                resp = await rpc.get_token_account_balance(ata, commitment=Confirmed)
                if resp.value is not None:
                    amount = int(resp.value.amount)
                    if amount > 0:
                        return amount
            except Exception:
                pass
            if attempt < 7:
                await asyncio.sleep(3)
        return 0
    except Exception:
        return 0

# ----------------------------------------------------------------------
# Token Age
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

# ----------------------------------------------------------------------
# Safety Check
# ----------------------------------------------------------------------

async def safety_check(mint: str, rpc: AsyncClient) -> bool:
    age = await get_token_age(mint)
    if age is not None:
        if age < MIN_TOKEN_AGE:
            logger.info(f"Too new: {age:.0f}s - {mint[:8]}...")
            return False
        if age > MAX_TOKEN_AGE:
            logger.info(f"Too old: {age/60:.0f}min - {mint[:8]}...")
            return False
        logger.info(f"Age: {age:.0f}s ✅")
    
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
                            logger.info(f"Risk: {risk.get('name')} (level {level})")
                            return False
    except Exception:
        pass
    
    try:
        mint_pub = Pubkey.from_string(mint)
        acc = await rpc.get_account_info(mint_pub, commitment=Confirmed)
        if acc.value is None:
            return False
        data = acc.value.data
        if len(data) < 4:
            return False
        if int.from_bytes(data[0:4], "little") != 0:
            logger.info(f"Mint authority active - {mint[:8]}...")
            return False
    except Exception:
        return False
    
    return True

# ----------------------------------------------------------------------
# Buy
# ----------------------------------------------------------------------

async def buy_token(mint: str, wallet: Keypair, sol_amount: float, slippage_bps: int) -> Tuple[str, VersionedTransaction]:
    amount_lamports = int(sol_amount * 1e9)
    quote = await jupiter_quote(WSOL_MINT, mint, amount_lamports, slippage_bps)
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    swap_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    swap_tx = sign_tx(swap_tx, wallet)
    txid = await send_tx(swap_tx)
    return txid, swap_tx

# ----------------------------------------------------------------------
# Sell
# ----------------------------------------------------------------------

async def sell_token(mint: str, wallet: Keypair, token_amount: int, slippage_bps: int) -> Tuple[str, VersionedTransaction]:
    quote = await jupiter_quote(mint, WSOL_MINT, token_amount, slippage_bps)
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    sell_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))
    sell_tx = sign_tx(sell_tx, wallet)
    txid = await send_tx(sell_tx)
    return txid, sell_tx

# ----------------------------------------------------------------------
# Backup DexScreener Watcher
# ----------------------------------------------------------------------

async def dex_price_watcher(mint: str, wallet: Keypair, token_amount: int, buy_price_sol: float, slippage_bps: int):
    """Backup: watches DexScreener for massive spikes every 1.5 seconds."""
    await asyncio.sleep(2)
    logger.info(f"👁️ DexScreener watcher started for {mint[:8]}...")
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("pairs"):
                            pair = data["pairs"][0]
                            price_usd = float(pair.get("priceUsd", 0))
                            if price_usd > 0:
                                estimated_sol = price_usd / SOL_PRICE_USD
                                multiple = estimated_sol / buy_price_sol
                                
                                if multiple >= TAKE_PROFIT_MULTIPLE:
                                    logger.info(f"🚨🚨🚨 DEX WATCHER: {multiple:.1f}x detected! Emergency sell!")
                                    try:
                                        await sell_token(mint, wallet, token_amount, slippage_bps)
                                        remove_position(mint)
                                    except Exception as e:
                                        logger.error(f"Emergency sell failed: {e}")
                                    return
        except Exception:
            pass
        await asyncio.sleep(1.5)

# ----------------------------------------------------------------------
# Main Exit Monitor (1-second checks)
# ----------------------------------------------------------------------

async def exit_monitor(
    mint: str,
    wallet: Keypair,
    token_amount: int,
    buy_price_sol: float,
    slippage_bps: int,
    take_profit_multiple: float,
    trailing_stop_pct: float,
    stop_loss_factor: float,
    max_hold_seconds: int,
):
    """
    Ultra-fast exit monitor:
    - Checks price every 1 second
    - Sells IMMEDIATELY when current price hits target
    - Stop-loss at stop_loss_factor of buy price
    - Trailing stop after 1.2x
    """
    start_time = time.time()
    peak_sol = buy_price_sol
    last_quote_time = time.time()
    sold = False

    logger.info(f"🔍 MONITOR: {mint[:8]}... | Buy: {buy_price_sol:.6f} SOL | Target: {take_profit_multiple}x = {buy_price_sol * take_profit_multiple:.6f} SOL")

    while not sold:
        elapsed = time.time() - start_time
        
        # Time limit
        if elapsed > max_hold_seconds:
            logger.info(f"⏰ Time limit ({elapsed:.0f}s), selling")
            try:
                await sell_token(mint, wallet, token_amount, slippage_bps)
            except Exception as e:
                logger.error(f"Sell failed: {e}")
            remove_position(mint)
            return
        
        # Quote timeout
        if time.time() - last_quote_time > FORCE_SELL_TIMEOUT:
            logger.warning(f"⚠️ No quote for {FORCE_SELL_TIMEOUT}s, force selling")
            try:
                await sell_token(mint, wallet, token_amount, slippage_bps)
            except Exception as e:
                logger.error(f"Force sell failed: {e}")
            remove_position(mint)
            return
        
        # Get price
        try:
            quote = await jupiter_quote(mint, WSOL_MINT, token_amount, slippage_bps)
            current_sol = int(quote['outAmount']) / 1e9
            last_quote_time = time.time()
        except Exception as e:
            await asyncio.sleep(1)
            continue
        
        # Track peak
        if current_sol > peak_sol:
            peak_sol = current_sol
            logger.info(f"🔺 New peak: {peak_sol:.6f} SOL ({peak_sol/buy_price_sol:.2f}x)")
        
        cm = current_sol / buy_price_sol  # current multiple
        pm = peak_sol / buy_price_sol      # peak multiple
        
        logger.info(f"📊 {cm:.2f}x | peak: {pm:.2f}x | target: {take_profit_multiple}x | stop: {stop_loss_factor*100:.0f}%")
        
        # STOP-LOSS
        if current_sol <= buy_price_sol * stop_loss_factor:
            logger.info(f"🛑 STOP-LOSS ({cm*100:.0f}% of buy), selling all")
            try:
                await sell_token(mint, wallet, token_amount, slippage_bps)
            except Exception as e:
                logger.error(f"Stop-loss failed: {e}")
            remove_position(mint)
            return
        
        # TAKE PROFIT - current price (instant!)
        if current_sol >= buy_price_sol * take_profit_multiple:
            profit = current_sol - buy_price_sol - (GAS_PER_TX_ESTIMATE * 2)
            logger.info(f"💰💰💰 TAKE PROFIT! {cm:.2f}x! Profit: ~{profit:.6f} SOL")
            try:
                await sell_token(mint, wallet, token_amount, slippage_bps)
            except Exception as e:
                logger.error(f"Take profit failed: {e}")
            remove_position(mint)
            return
        
        # TRAILING STOP (after 1.2x)
        if pm >= 1.2:
            drop = (1 - current_sol / peak_sol) * 100
            if drop >= trailing_stop_pct:
                logger.info(f"📉 Trailing stop: drop {drop:.1f}% from peak {pm:.2f}x")
                try:
                    await sell_token(mint, wallet, token_amount, slippage_bps)
                except Exception as e:
                    logger.error(f"Trailing stop failed: {e}")
                remove_position(mint)
                return
        
        await asyncio.sleep(PRICE_CHECK_INTERVAL)  # 1 second

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
    logger.info("⚡ Solana Meme Sniper Bot - ULTRA FAST")
    logger.info(f"Wallet: {WALLET.pubkey()}")
    logger.info(f"Buy: {SNIPE_AMOUNT_SOL} SOL | Slippage: {SLIPPAGE_BPS/100:.0f}%")
    logger.info(f"Target: {TAKE_PROFIT_MULTIPLE}x (sell 100%)")
    logger.info(f"Stop-loss: {(1-STOP_LOSS_FACTOR)*100:.0f}% max loss")
    logger.info(f"Price check: every {PRICE_CHECK_INTERVAL}s")
    logger.info(f"DexScreener backup watcher: enabled")
    logger.info("=" * 60)

    rpc = await create_rpc()
    balance = await get_wallet_balance(rpc)
    logger.info(f"Balance: {balance:.4f} SOL | Available: {max(0, balance - GAS_RESERVE_SOL):.4f} SOL")
    logger.info("=" * 60)

    # Resume positions
    positions = load_positions()
    if positions:
        logger.info(f"📂 Resuming {len(positions)} positions...")
        for mint, data in positions.items():
            t1 = asyncio.create_task(exit_monitor(
                mint=mint, wallet=WALLET, token_amount=data["token_amount"],
                buy_price_sol=data["buy_price_sol"], slippage_bps=SLIPPAGE_BPS,
                take_profit_multiple=TAKE_PROFIT_MULTIPLE, trailing_stop_pct=TRAILING_STOP_PCT,
                stop_loss_factor=STOP_LOSS_FACTOR, max_hold_seconds=MAX_HOLD_SECONDS,
            ))
            t2 = asyncio.create_task(dex_price_watcher(
                mint=mint, wallet=WALLET, token_amount=data["token_amount"],
                buy_price_sol=data["buy_price_sol"], slippage_bps=SLIPPAGE_BPS,
            ))
            ACTIVE_MONITORS[mint] = [t1, t2]
        logger.info("=" * 60)

    async for mint in discover_new_tokens():
        balance = await get_wallet_balance(rpc)
        
        if not can_afford_buy(balance):
            logger.warning(f"⚠️ Low balance ({balance:.4f} SOL). Waiting 30s...")
            await asyncio.sleep(30)
            continue
        
        if mint in ACTIVE_MONITORS:
            continue
        
        logger.info(f"🆕 {mint}")

        if not await safety_check(mint, rpc):
            continue

        logger.info(f"✅ Sniping {mint[:8]}...")

        try:
            txid, _ = await buy_token(mint, WALLET, SNIPE_AMOUNT_SOL, SLIPPAGE_BPS)
            logger.info(f"✅ Bought! TX: {txid[:40]}...")
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            continue

        token_amount = await get_token_balance(rpc, WALLET.pubkey(), Pubkey.from_string(mint))
        if token_amount == 0:
            logger.warning("No tokens received")
            continue

        logger.info(f"✅ {token_amount} tokens received. Monitoring...")
        save_position(mint, token_amount, SNIPE_AMOUNT_SOL)

        # Start dual monitors
        t1 = asyncio.create_task(exit_monitor(
            mint=mint, wallet=WALLET, token_amount=token_amount,
            buy_price_sol=SNIPE_AMOUNT_SOL, slippage_bps=SLIPPAGE_BPS,
            take_profit_multiple=TAKE_PROFIT_MULTIPLE, trailing_stop_pct=TRAILING_STOP_PCT,
            stop_loss_factor=STOP_LOSS_FACTOR, max_hold_seconds=MAX_HOLD_SECONDS,
        ))
        t2 = asyncio.create_task(dex_price_watcher(
            mint=mint, wallet=WALLET, token_amount=token_amount,
            buy_price_sol=SNIPE_AMOUNT_SOL, slippage_bps=SLIPPAGE_BPS,
        ))
        ACTIVE_MONITORS[mint] = [t1, t2]

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
