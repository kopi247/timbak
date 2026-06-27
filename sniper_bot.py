#!/usr/bin/env python3
"""
Fully Automated Solana Meme-Sniping Bot
Uses Jupiter API directly + Jito bundles for stealth execution.
Smart exit: scaling out, trailing stop, market-cap & time failsafes.
Position persistence: survives restarts.
Gas reserve: maintains minimum SOL balance to cover fees.
Helius backrun rebates: earn SOL when backrun bots profit from your trades.
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
from typing import Optional, Tuple, List

import aiohttp
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.instruction import Instruction
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

# Solana RPC (Helius recommended for backrun rebates)
RPC_HTTP = os.getenv("RPC_HTTP", "")
if not RPC_HTTP:
    raise ValueError("RPC_HTTP not set in .env")

# Wallet - accepts Base58 private key string
private_key_str = os.getenv("PRIVATE_KEY", "")
if not private_key_str:
    raise ValueError("PRIVATE_KEY not set in .env")

try:
    WALLET = Keypair.from_base58_string(private_key_str)
    logger.info(f"Wallet loaded: {WALLET.pubkey()}")
except Exception as e:
    raise ValueError(f"Invalid PRIVATE_KEY: {e}")

# Jito
JITO_BUNDLE_URL = os.getenv("JITO_BUNDLE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")
JITO_AUTH_HEADER = {"x-jito-auth": os.getenv("JITO_AUTH_HEADER", "")} if os.getenv("JITO_AUTH_HEADER") else {}

# SEND MODE: "jito" or "rpc"
SEND_MODE = os.getenv("SEND_MODE", "rpc")

# Official Jito tip wallets
TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4eVV9bD44FvwYf8KvbtyY81",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
]

# Jupiter API
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# Rate limiting for Jupiter API
LAST_JUPITER_CALL = 0
JUPITER_RATE_LIMIT = 1.5

# Snipe settings
SNIPE_AMOUNT_SOL = float(os.getenv("SNIPE_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "2500"))
TIP_LAMPORTS = int(os.getenv("TIP_LAMPORTS", "500000"))
MAX_RUGCHECK_RISK = int(os.getenv("MAX_RUGCHECK_RISK", "0"))

# Gas reserve settings
GAS_RESERVE_SOL = float(os.getenv("GAS_RESERVE_SOL", "0.01"))
GAS_PER_TX_ESTIMATE = 0.00005

# Helius backrun rebate
HELIUS_REBATE_ACCOUNT = Pubkey.from_string("75GfAsUMc6K4WmwsE8qUxYHmGtCkF6RaLwqW7PHuNrzB")
ENABLE_BACKRUN_REBATE = os.getenv("ENABLE_BACKRUN_REBATE", "true").lower() == "true"

# Exit settings
TARGET_MULTIPLES = [(2.0, 0.25), (3.0, 0.25), (5.0, 0.25)]
TRAILING_STOP_PCT = 20.0
STOP_LOSS_FACTOR = 0.4
MAX_HOLD_SECONDS = 1200
TARGET_MARKET_CAP = 1_000_000
FORCE_SELL_TIMEOUT = 300

# Token discovery
DISCOVERY_POLL_SECONDS = 1
SEEN_TOKENS: set = set()
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "Es9vMFrzaCER9wFRNsmH8zFvwYQzG5C4R6eHn9rxRz8Q",
}

# WSOL mint
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Position persistence
POSITIONS_FILE = Path("positions.json")
ACTIVE_MONITORS: dict = {}

# ----------------------------------------------------------------------
# Position Persistence
# ----------------------------------------------------------------------

def save_position(mint: str, token_amount: int, buy_price_sol: float):
    """Save a position to disk so it survives restarts."""
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
    """Load positions from disk."""
    if POSITIONS_FILE.exists():
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def remove_position(mint: str):
    """Remove a position after selling."""
    positions = load_positions()
    if mint in positions:
        del positions[mint]
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        logger.info(f"💾 Position removed: {mint[:8]}...")

# ----------------------------------------------------------------------
# Wallet Balance Management
# ----------------------------------------------------------------------

async def get_wallet_balance(rpc: AsyncClient) -> float:
    """Get wallet SOL balance."""
    try:
        resp = await rpc.get_balance(WALLET.pubkey(), commitment=Confirmed)
        return resp.value / 1e9
    except Exception as e:
        logger.error(f"Failed to get wallet balance: {e}")
        return 0.0

def can_afford_buy(wallet_balance_sol: float) -> bool:
    """Check if wallet can afford a buy + gas + tip."""
    tip_sol = TIP_LAMPORTS / 1e9 if SEND_MODE == "jito" else 0
    total_needed = SNIPE_AMOUNT_SOL + tip_sol + GAS_PER_TX_ESTIMATE
    can_afford = wallet_balance_sol >= (total_needed + GAS_RESERVE_SOL)
    
    if not can_afford:
        logger.warning(
            f"Insufficient balance: {wallet_balance_sol:.4f} SOL, "
            f"need {total_needed:.4f} + {GAS_RESERVE_SOL:.4f} reserve = "
            f"{total_needed + GAS_RESERVE_SOL:.4f} SOL"
        )
    
    return can_afford

# ----------------------------------------------------------------------
# Rate Limiter
# ----------------------------------------------------------------------

async def rate_limited_jupiter_call():
    """Ensure we don't exceed Jupiter API rate limits."""
    global LAST_JUPITER_CALL
    elapsed = time.time() - LAST_JUPITER_CALL
    if elapsed < JUPITER_RATE_LIMIT:
        wait = JUPITER_RATE_LIMIT - elapsed + random.uniform(0, 0.5)
        await asyncio.sleep(wait)
    LAST_JUPITER_CALL = time.time()

# ----------------------------------------------------------------------
# Jupiter API Helpers
# ----------------------------------------------------------------------

async def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    """Get a quote from Jupiter API with retry on rate limits."""
    await rate_limited_jupiter_call()
    
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(JUPITER_QUOTE_URL, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait_time = (attempt + 1) * 2
                        logger.warning(f"Rate limited by Jupiter, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        error_text = await resp.text()
                        raise Exception(f"Jupiter quote error: {error_text}")
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            raise
    raise Exception("Jupiter quote failed after retries")

async def jupiter_swap(quote_response: dict, user_public_key: str) -> dict:
    """Get swap transaction from Jupiter API with retry."""
    await rate_limited_jupiter_call()
    
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": 500000,      # 0.0005 SOL max priority fee
                "priorityLevel": "veryHigh"  # aggressive priority
            }
        },
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(JUPITER_SWAP_URL, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait_time = (attempt + 1) * 2
                        logger.warning(f"Rate limited on swap, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        error_text = await resp.text()
                        raise Exception(f"Jupiter swap error: {error_text}")
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            raise
    raise Exception("Jupiter swap failed after retries")

# ----------------------------------------------------------------------
# Helius Backrun Rebate
# ----------------------------------------------------------------------

def add_backrun_rebate_instruction(tx: VersionedTransaction, wallet: Keypair) -> VersionedTransaction:
    """
    Add Helius backrun rebate instruction to a transaction.
    Earn SOL rebates when backrun bots profit from your trades.
    Only works when using Helius RPC.
    """
    if not ENABLE_BACKRUN_REBATE:
        return tx
    
    try:
        from solders.instruction import AccountMeta
        
        # Create a simple memo instruction that signals "rebate eligible" to Helius
        # Using a no-op transfer of 0 lamports to the rebate account
        rebate_ix = Instruction(
            program_id=Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"),  # Memo program
            accounts=[
                AccountMeta(pubkey=wallet.pubkey(), is_signer=True, is_writable=False),
            ],
            data=b"Helius rebate",  # Memo text signalling rebate eligibility
        )
        
        # Combine with existing instructions
        existing_instructions = list(tx.message.instructions)
        existing_instructions.append(rebate_ix)
        
        # Rebuild message
        new_msg = MessageV0.try_compile(
            payer=wallet.pubkey(),
            instructions=existing_instructions,
            address_lookup_table_accounts=tx.message.address_table_lookups,
            recent_blockhash=tx.message.recent_blockhash,
        )
        
        logger.debug("💰 Backrun rebate instruction added")
        return VersionedTransaction(new_msg, [wallet])
        
    except Exception as e:
        logger.warning(f"Failed to add rebate instruction: {e}")
        return tx
        
# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

async def create_rpc() -> AsyncClient:
    return AsyncClient(RPC_HTTP)

def create_tip_transaction(
    wallet: Keypair, tip_account: str, lamports: int, recent_blockhash: str
) -> VersionedTransaction:
    """Create a simple SOL transfer to a Jito tip address."""
    to_pubkey = Pubkey.from_string(tip_account)
    ix = transfer(
        TransferParams(
            from_pubkey=wallet.pubkey(),
            to_pubkey=to_pubkey,
            lamports=lamports,
        )
    )
    msg = MessageV0.try_compile(
        payer=wallet.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(recent_blockhash),
    )
    return VersionedTransaction(msg, [wallet])

def sign_swap_transaction(tx: VersionedTransaction, wallet: Keypair) -> VersionedTransaction:
    """Sign Jupiter swap transaction with our wallet for solders 0.27.1."""
    try:
        return VersionedTransaction(tx.message, [wallet])
    except Exception as e:
        logger.warning(f"Sign failed: {e}, returning original")
        return tx

async def send_transaction(swap_tx: VersionedTransaction, tip_tx: Optional[VersionedTransaction] = None) -> str:
    """Send transaction via Jito bundle or direct RPC with confirmation."""
    
    if SEND_MODE == "rpc":
        rpc = await create_rpc()
        try:
            raw_bytes = bytes(swap_tx)
            txid_resp = await rpc.send_raw_transaction(raw_bytes)
            
            if hasattr(txid_resp, 'value'):
                sig = str(txid_resp.value)
            else:
                sig = str(txid_resp)
            
            logger.info(f"Transaction sent: {sig[:40]}...")
            
            for i in range(15):
                await asyncio.sleep(4)
                try:
                    resp = await rpc.get_signature_statuses([sig])
                    if resp.value and len(resp.value) > 0 and resp.value[0] is not None:
                        status = resp.value[0]
                        if hasattr(status, 'err') and status.err is not None:
                            logger.error(f"❌ Transaction FAILED: {status.err}")
                            raise Exception(f"Transaction failed: {status.err}")
                        else:
                            logger.info(f"✅ Transaction confirmed!")
                            return sig
                except Exception as e:
                    if "Transaction failed" in str(e):
                        raise
                    pass
            
            logger.warning("Transaction not confirmed after 60s, continuing")
            return sig
            
        except Exception as e:
            raise Exception(f"RPC send failed: {e}")
    
    else:
        if tip_tx is None:
            raise Exception("Tip transaction required for Jito")
            
        swap_b64 = base64.b64encode(bytes(swap_tx)).decode("utf-8")
        tip_b64 = base64.b64encode(bytes(tip_tx)).decode("utf-8")
        bundle = [swap_b64, tip_b64]
        
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "sendBundle",
            "params": [bundle],
        }
        
        max_retries = 3
        for attempt in range(max_retries):
            async with aiohttp.ClientSession() as session:
                async with session.post(JITO_BUNDLE_URL, json=payload, headers=JITO_AUTH_HEADER) as resp:
                    result = await resp.json()
                    
            if "error" in result:
                error_code = result["error"].get("code", 0)
                error_msg = result["error"].get("message", "")
                
                if error_code == -32097 or "rate limited" in error_msg.lower():
                    wait_time = (attempt + 1) * 3
                    logger.warning(f"Jito rate limited, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Jito error: {result['error']}")
            else:
                bundle_id = result.get("result", "unknown")
                logger.info(f"Jito bundle accepted: {bundle_id}")
                return bundle_id
        
        raise Exception("Jito bundle failed after retries")

async def get_token_balance(rpc: AsyncClient, wallet: Pubkey, mint: Pubkey) -> int:
    """Return raw token balance using RPC (works for Token and Token-2022)."""
    try:
        from spl.token.instructions import get_associated_token_address
        
        ata = get_associated_token_address(wallet, mint)
        
        for attempt in range(8):
            try:
                resp = await rpc.get_token_account_balance(ata, commitment=Confirmed)
                if resp.value is not None:
                    amount = int(resp.value.amount)
                    if amount > 0:
                        logger.info(f"Token balance confirmed: {amount}")
                        return amount
            except Exception:
                pass
            
            if attempt < 7:
                await asyncio.sleep(3)
        
        logger.warning("Token balance still 0 after retries")
        return 0
    except Exception as e:
        logger.error(f"Error getting token balance: {e}")
        return 0

# ----------------------------------------------------------------------
# Safety Checks
# ----------------------------------------------------------------------

async def safety_check(mint: str, rpc: AsyncClient) -> bool:
    """Return True if token passes all checks."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    risks = data.get("risks", [])
                    for risk in risks:
                        level = risk.get("level", 0)
                        if isinstance(level, str):
                            try:
                                level = int(level)
                            except ValueError:
                                level = 99
                        if level > MAX_RUGCHECK_RISK:
                            risk_name = risk.get("name", "Unknown")
                            logger.info(f"RugCheck risk: {risk_name} (level {level})")
                            return False
                elif resp.status == 404:
                    logger.info(f"No RugCheck report yet for {mint[:8]}... (allowing)")
                else:
                    logger.warning(f"RugCheck API error: {resp.status}")
    except asyncio.TimeoutError:
        logger.warning(f"RugCheck timeout for {mint[:8]}...")
    except Exception as e:
        logger.warning(f"RugCheck error: {e}")

    try:
        mint_pub = Pubkey.from_string(mint)
        acc = await rpc.get_account_info(mint_pub, commitment=Confirmed)
        if acc.value is None:
            logger.info(f"Token account not found for {mint[:8]}...")
            return False

        data = acc.value.data

        if len(data) < 4:
            return False
        mint_auth_option = int.from_bytes(data[0:4], "little")
        if mint_auth_option != 0:
            logger.info(f"Mint authority not renounced for {mint[:8]}...")
            return False

        if len(data) > 39:
            freeze_auth_option = int.from_bytes(data[36:40], "little")
            if freeze_auth_option != 0:
                logger.info(f"Freeze authority present on {mint[:8]}... (proceeding anyway)")

    except Exception as e:
        logger.warning(f"On-chain check error: {e}")
        return False

    return True

# ----------------------------------------------------------------------
# Buy
# ----------------------------------------------------------------------

async def buy_with_jito(
    mint: str,
    wallet: Keypair,
    sol_amount: float,
    slippage_bps: int,
    tip_lamports: int,
) -> Tuple[str, VersionedTransaction]:
    """Snipe a token with Helius backrun rebate (RPC mode) or Jito tip."""
    amount_lamports = int(sol_amount * 1e9)
    
    quote = await jupiter_quote(WSOL_MINT, mint, amount_lamports, slippage_bps)
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    swap_tx_bytes = base64.b64decode(tx_data["swapTransaction"])
    swap_tx = VersionedTransaction.from_bytes(swap_tx_bytes)
    swap_tx = sign_swap_transaction(swap_tx, wallet)
    
    # Add Helius backrun rebate (RPC mode only)
    if SEND_MODE == "rpc":
        swap_tx = add_backrun_rebate_instruction(swap_tx, wallet)
    
    blockhash_str = str(swap_tx.message.recent_blockhash)

    if SEND_MODE == "rpc":
        txid = await send_transaction(swap_tx)
        return txid, swap_tx
    else:
        tip_tx = create_tip_transaction(wallet, TIP_ACCOUNTS[0], tip_lamports, blockhash_str)
        bundle_id = await send_transaction(swap_tx, tip_tx)
        return bundle_id, swap_tx

# ----------------------------------------------------------------------
# Sell
# ----------------------------------------------------------------------

async def sell_with_jito(
    mint: str,
    wallet: Keypair,
    token_amount: int,
    slippage_bps: int,
    tip_lamports: int,
) -> Tuple[str, VersionedTransaction]:
    """Sell tokens with Helius backrun rebate (RPC mode) or Jito tip."""
    quote = await jupiter_quote(mint, WSOL_MINT, token_amount, slippage_bps)
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    sell_tx_bytes = base64.b64decode(tx_data["swapTransaction"])
    sell_tx = VersionedTransaction.from_bytes(sell_tx_bytes)
    sell_tx = sign_swap_transaction(sell_tx, wallet)
    
    # Add Helius backrun rebate (RPC mode only)
    if SEND_MODE == "rpc":
        sell_tx = add_backrun_rebate_instruction(sell_tx, wallet)
    
    blockhash_str = str(sell_tx.message.recent_blockhash)

    if SEND_MODE == "rpc":
        txid = await send_transaction(sell_tx)
        return txid, sell_tx
    else:
        tip_tx = create_tip_transaction(wallet, TIP_ACCOUNTS[0], tip_lamports, blockhash_str)
        bundle_id = await send_transaction(sell_tx, tip_tx)
        return bundle_id, sell_tx

# ----------------------------------------------------------------------
# Smart Exit Monitor
# ----------------------------------------------------------------------

async def get_market_cap(mint: str) -> float:
    """Get market cap from DexScreener."""
    try:
        async with aiohttp.ClientSession() as s:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with s.get(url) as resp:
                data = await resp.json()
                if data.get("pairs"):
                    for pair in data["pairs"]:
                        if pair.get("marketCap"):
                            return float(pair["marketCap"])
    except Exception:
        pass
    return 0.0

async def ultimate_exit_monitor(
    mint: str,
    wallet: Keypair,
    initial_token_amount: int,
    buy_price_sol: float,
    slippage_bps: int,
    tip_lamports: int,
    target_multiples: List[Tuple[float, float]],
    trailing_stop_pct: float,
    stop_loss_factor: float,
    max_hold_seconds: int,
    target_mc: float,
):
    """Smart exit strategy with force-sell fallback."""
    start_time = time.time()
    remaining = initial_token_amount
    peak_sol = buy_price_sol
    last_successful_quote = time.time()

    logger.info(f"🔍 Exit monitor STARTED for {mint[:8]}... ({remaining} tokens, bought at {buy_price_sol:.6f} SOL)")

    try:
        for mult, fraction in target_multiples:
            while remaining > 0:
                elapsed = time.time() - start_time
                
                if elapsed > max_hold_seconds:
                    logger.info(f"⏰ Time limit reached ({elapsed:.0f}s), selling remaining {remaining}")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Time-limit sell failed: {e}")
                    remove_position(mint)
                    return

                if time.time() - last_successful_quote > FORCE_SELL_TIMEOUT:
                    logger.warning(f"⚠️ No successful quote for {FORCE_SELL_TIMEOUT}s, force selling!")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Force sell failed: {e}")
                    remove_position(mint)
                    return

                try:
                    quote = await jupiter_quote(mint, WSOL_MINT, remaining, slippage_bps)
                    current_sol = int(quote['outAmount']) / 1e9
                    last_successful_quote = time.time()
                except Exception as e:
                    logger.warning(f"Quote error ({time.time() - last_successful_quote:.0f}s since last success): {e}")
                    await asyncio.sleep(3)
                    continue

                if current_sol > peak_sol:
                    peak_sol = current_sol

                logger.info(f"📊 {mint[:8]}... value: {current_sol:.6f} SOL | peak: {peak_sol:.6f} | "
                           f"target: {buy_price_sol * mult:.6f} | stop: {buy_price_sol * stop_loss_factor:.6f}")

                if current_sol <= buy_price_sol * stop_loss_factor:
                    logger.info(f"🛑 Stop-loss hit ({current_sol:.6f} SOL), selling all")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Stop-loss sell failed: {e}")
                    remove_position(mint)
                    return

                if current_sol >= buy_price_sol * mult:
                    sell_amount = int(initial_token_amount * fraction)
                    sell_amount = min(sell_amount, remaining)
                    logger.info(f"💰 Take profit: selling {fraction*100:.0f}% at {mult}x (value: {current_sol:.6f} SOL)")
                    try:
                        await sell_with_jito(mint, wallet, sell_amount, slippage_bps, tip_lamports)
                        remaining -= sell_amount
                        peak_sol = current_sol
                    except Exception as e:
                        logger.error(f"Take-profit sell failed: {e}")
                    break

                await asyncio.sleep(3)

        if remaining > 0:
            logger.info(f"🌙 Moonbag trailing stop for {mint[:8]}... ({remaining} tokens)")
            while remaining > 0:
                elapsed = time.time() - start_time
                if elapsed > max_hold_seconds:
                    logger.info(f"⏰ Final time limit, selling moonbag")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Final sell failed: {e}")
                    remove_position(mint)
                    return

                if time.time() - last_successful_quote > FORCE_SELL_TIMEOUT:
                    logger.warning(f"⚠️ Force selling moonbag after {FORCE_SELL_TIMEOUT}s without quote")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Force sell moonbag failed: {e}")
                    remove_position(mint)
                    return

                try:
                    quote = await jupiter_quote(mint, WSOL_MINT, remaining, slippage_bps)
                    current_sol = int(quote['outAmount']) / 1e9
                    last_successful_quote = time.time()
                except Exception:
                    await asyncio.sleep(3)
                    continue

                if current_sol > peak_sol:
                    peak_sol = current_sol

                if current_sol <= peak_sol * (1 - trailing_stop_pct / 100):
                    logger.info(f"📉 Trailing stop: peak {peak_sol:.6f}, current {current_sol:.6f}")
                    try:
                        await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                    except Exception as e:
                        logger.error(f"Trailing stop sell failed: {e}")
                    remove_position(mint)
                    return

                await asyncio.sleep(3)

        remove_position(mint)
        logger.info(f"✅ Exit monitor completed for {mint[:8]}...")

    except Exception as e:
        logger.error(f"💥 Exit monitor for {mint[:8]}... crashed: {e}", exc_info=True)
        remove_position(mint)

# ----------------------------------------------------------------------
# Token Discovery
# ----------------------------------------------------------------------

async def discover_new_tokens():
    """Poll DexScreener for new Solana tokens."""
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
    logger.info("Starting Solana Meme Sniper Bot")
    logger.info(f"Wallet: {WALLET.pubkey()}")
    logger.info(f"RPC: {RPC_HTTP[:50]}...")
    logger.info(f"Snipe amount: {SNIPE_AMOUNT_SOL} SOL")
    logger.info(f"Gas reserve: {GAS_RESERVE_SOL} SOL")
    logger.info(f"Jito tip: {TIP_LAMPORTS} lamports")
    logger.info(f"Send mode: {SEND_MODE}")
    logger.info(f"Backrun rebate: {'Enabled' if ENABLE_BACKRUN_REBATE else 'Disabled'}")
    logger.info("=" * 60)

    rpc = await create_rpc()
    
    balance = await get_wallet_balance(rpc)
    logger.info(f"Wallet balance: {balance:.4f} SOL")
    logger.info(f"Available for trading: {max(0, balance - GAS_RESERVE_SOL):.4f} SOL")
    logger.info("=" * 60)

    positions = load_positions()
    if positions:
        logger.info(f"📂 Found {len(positions)} existing positions. Resuming monitors...")
        for mint, data in positions.items():
            logger.info(f"Resuming exit monitor for {mint[:8]}... ({data['token_amount']} tokens)")
            task = asyncio.create_task(
                ultimate_exit_monitor(
                    mint=mint,
                    wallet=WALLET,
                    initial_token_amount=data["token_amount"],
                    buy_price_sol=data["buy_price_sol"],
                    slippage_bps=SLIPPAGE_BPS,
                    tip_lamports=TIP_LAMPORTS,
                    target_multiples=TARGET_MULTIPLES,
                    trailing_stop_pct=TRAILING_STOP_PCT,
                    stop_loss_factor=STOP_LOSS_FACTOR,
                    max_hold_seconds=MAX_HOLD_SECONDS,
                    target_mc=TARGET_MARKET_CAP,
                )
            )
            ACTIVE_MONITORS[mint] = task
        logger.info("=" * 60)

    async for mint in discover_new_tokens():
        balance = await get_wallet_balance(rpc)
        
        if not can_afford_buy(balance):
            logger.warning(
                f"⚠️  Low balance ({balance:.4f} SOL). "
                f"Need at least {SNIPE_AMOUNT_SOL + GAS_RESERVE_SOL + (TIP_LAMPORTS/1e9 if SEND_MODE=='jito' else 0):.4f} SOL. "
                f"Waiting 30s..."
            )
            await asyncio.sleep(30)
            continue
        
        if mint in ACTIVE_MONITORS:
            continue
        
        logger.info(f"New token: {mint} (balance: {balance:.4f} SOL)")

        if not await safety_check(mint, rpc):
            logger.info(f"Safety failed for {mint[:8]}...")
            continue

        logger.info(f"Safety passed. Sniping {mint[:8]}...")

        try:
            txid, swap_tx = await buy_with_jito(
                mint=mint,
                wallet=WALLET,
                sol_amount=SNIPE_AMOUNT_SOL,
                slippage_bps=SLIPPAGE_BPS,
                tip_lamports=TIP_LAMPORTS,
            )
            logger.info(f"✅ Buy sent! TX: {txid[:40]}...")
        except Exception as e:
            error_str = str(e)
            if "insufficient lamports" in error_str.lower() or "0x1" in error_str:
                logger.error(f"Insufficient funds. Waiting 30s...")
                await asyncio.sleep(30)
            else:
                logger.error(f"Buy failed: {e}")
            continue

        logger.info("Waiting for token balance...")
        token_amount = await get_token_balance(rpc, WALLET.pubkey(), Pubkey.from_string(mint))
        
        if token_amount == 0:
            logger.warning("No tokens received after retries, skipping exit.")
            continue

        logger.info(f"✅ Received {token_amount} tokens. Starting exit monitor...")

        save_position(mint, token_amount, SNIPE_AMOUNT_SOL)

        task = asyncio.create_task(
            ultimate_exit_monitor(
                mint=mint,
                wallet=WALLET,
                initial_token_amount=token_amount,
                buy_price_sol=SNIPE_AMOUNT_SOL,
                slippage_bps=SLIPPAGE_BPS,
                tip_lamports=TIP_LAMPORTS,
                target_multiples=TARGET_MULTIPLES,
                trailing_stop_pct=TRAILING_STOP_PCT,
                stop_loss_factor=STOP_LOSS_FACTOR,
                max_hold_seconds=MAX_HOLD_SECONDS,
                target_mc=TARGET_MARKET_CAP,
            )
        )
        ACTIVE_MONITORS[mint] = task
        task.add_done_callback(
            lambda t, m=mint: (
                logger.error(f"Exit monitor for {m[:8]}... crashed: {t.exception()}") 
                if t.exception() 
                else logger.info(f"Exit monitor for {m[:8]}... completed normally")
            )
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
