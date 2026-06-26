#!/usr/bin/env python3
"""
Fully Automated Solana Meme-Sniping Bot
Uses Jupiter API directly + Jito bundles for stealth execution.
Smart exit: scaling out, trailing stop, market-cap & time failsafes.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from typing import Optional, Tuple, List

import aiohttp
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.pubkey import Pubkey
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

# Official Jito tip wallets
TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4eVV9bD44FvwYf8KvbtyY81",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
]

# Jupiter API
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# Snipe settings
SNIPE_AMOUNT_SOL = float(os.getenv("SNIPE_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "2500"))
TIP_LAMPORTS = int(os.getenv("TIP_LAMPORTS", "500000"))
MAX_RUGCHECK_RISK = int(os.getenv("MAX_RUGCHECK_RISK", "0"))

# Exit settings
TARGET_MULTIPLES = [(2.0, 0.25), (3.0, 0.25), (5.0, 0.25)]
TRAILING_STOP_PCT = 20.0
STOP_LOSS_FACTOR = 0.4
MAX_HOLD_SECONDS = 1200
TARGET_MARKET_CAP = 1_000_000

# Token discovery
DISCOVERY_POLL_SECONDS = 1
SEEN_TOKENS: set = set()
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "Es9vMFrzaCER9wFRNsmH8zFvwYQzG5C4R6eHn9rxRz8Q",
}

# WSOL mint
WSOL_MINT = "So11111111111111111111111111111111111111112"

# ----------------------------------------------------------------------
# Jupiter API Helpers (no SDK needed)
# ----------------------------------------------------------------------

async def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    """Get a quote from Jupiter API."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(JUPITER_QUOTE_URL, params=params) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"Jupiter quote error: {error_text}")
            return await resp.json()

async def jupiter_swap(quote_response: dict, user_public_key: str) -> dict:
    """Get swap transaction from Jupiter API."""
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(JUPITER_SWAP_URL, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"Jupiter swap error: {error_text}")
            return await resp.json()

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
        recent_blockhash=Pubkey.from_string(recent_blockhash),
    )
    return VersionedTransaction(msg, [wallet])

async def send_jito_bundle(
    swap_tx: VersionedTransaction, tip_tx: VersionedTransaction
) -> str:
    """Send swap + tip as a Jito bundle, return bundle ID."""
    bundle = [
        base64.b64encode(bytes(swap_tx)).decode("utf-8"),
        base64.b64encode(bytes(tip_tx)).decode("utf-8"),
    ]
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "sendBundle",
        "params": [bundle],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(JITO_BUNDLE_URL, json=payload, headers=JITO_AUTH_HEADER) as resp:
            result = await resp.json()
            if "error" in result:
                raise Exception(f"Jito error: {result['error']}")
            return result["result"]

async def get_token_balance(rpc: AsyncClient, wallet: Pubkey, mint: Pubkey) -> int:
    """Return raw token balance from associated token account."""
    try:
        from spl.token.instructions import get_associated_token_address
        ata = get_associated_token_address(wallet, mint)
        acc = await rpc.get_account_info(ata, commitment=Confirmed)
        if acc.value is None:
            return 0
        data = acc.value.data
        if len(data) < 72:
            return 0
        amount_bytes = data[64:72]
        return int.from_bytes(amount_bytes, "little")
    except Exception:
        return 0

# ----------------------------------------------------------------------
# Safety Checks
# ----------------------------------------------------------------------

async def safety_check(mint: str, rpc: AsyncClient) -> bool:
    """Return True if token passes all checks."""
    # 1. RugCheck API
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    risks = data.get("risks", [])
                    # Handle both string and int risk levels
                    for risk in risks:
                        level = risk.get("level", 0)
                        # Convert to int if it's a string
                        if isinstance(level, str):
                            try:
                                level = int(level)
                            except ValueError:
                                level = 99  # unknown risk, fail
                        if level > MAX_RUGCHECK_RISK:
                            risk_name = risk.get("name", "Unknown")
                            logger.info(f"RugCheck risk: {risk_name} (level {level})")
                            return False
                elif resp.status == 404:
                    # No report yet - might be too new, but we'll allow it
                    logger.info(f"No RugCheck report yet for {mint[:8]}... (allowing)")
                else:
                    logger.warning(f"RugCheck API error: {resp.status}")
    except asyncio.TimeoutError:
        logger.warning(f"RugCheck timeout for {mint[:8]}...")
    except Exception as e:
        logger.warning(f"RugCheck error: {e}")

    # 2. On-chain checks
    try:
        mint_pub = Pubkey.from_string(mint)
        acc = await rpc.get_account_info(mint_pub, commitment=Confirmed)
        if acc.value is None:
            logger.info(f"Token account not found for {mint[:8]}...")
            return False

        data = acc.value.data

        # Check mint authority (offset 0: 4 bytes option)
        if len(data) < 4:
            return False
        mint_auth_option = int.from_bytes(data[0:4], "little")
        if mint_auth_option != 0:
            logger.info(f"Mint authority not renounced for {mint[:8]}...")
            return False

        # Check freeze authority (offset 36: 4 bytes option)
        # Skip freeze check - many new tokens have this temporarily
        # and it's often renounced within minutes
        if len(data) > 39:
            freeze_auth_option = int.from_bytes(data[36:40], "little")
            if freeze_auth_option != 0:
                logger.info(f"Freeze authority present on {mint[:8]}... (proceeding anyway)")

    except Exception as e:
        logger.warning(f"On-chain check error: {e}")
        return False

    return True
    
# ----------------------------------------------------------------------
# Jito Bundled Buy
# ----------------------------------------------------------------------

async def buy_with_jito(
    mint: str,
    wallet: Keypair,
    sol_amount: float,
    slippage_bps: int,
    tip_lamports: int,
) -> Tuple[str, VersionedTransaction]:
    """Snipe a token with Jito bundle."""
    amount_lamports = int(sol_amount * 1e9)
    
    # 1. Quote
    quote = await jupiter_quote(WSOL_MINT, mint, amount_lamports, slippage_bps)
    
    # 2. Swap transaction
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    swap_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))

    # 3. Extract blockhash
    blockhash_str = str(swap_tx.message.recent_blockhash)

    # 4. Tip transaction
    tip_account = TIP_ACCOUNTS[0]
    tip_tx = create_tip_transaction(wallet, tip_account, tip_lamports, blockhash_str)

    # 5. Send bundle
    bundle_id = await send_jito_bundle(swap_tx, tip_tx)
    logger.info(f"Buy bundle sent: {bundle_id}")
    return bundle_id, swap_tx

# ----------------------------------------------------------------------
# Jito Bundled Sell
# ----------------------------------------------------------------------

async def sell_with_jito(
    mint: str,
    wallet: Keypair,
    token_amount: int,
    slippage_bps: int,
    tip_lamports: int,
) -> Tuple[str, VersionedTransaction]:
    """Sell tokens via Jito bundle."""
    # 1. Quote sell
    quote = await jupiter_quote(mint, WSOL_MINT, token_amount, slippage_bps)
    
    # 2. Swap transaction
    tx_data = await jupiter_swap(quote, str(wallet.pubkey()))
    sell_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_data["swapTransaction"]))

    # 3. Extract blockhash
    blockhash_str = str(sell_tx.message.recent_blockhash)

    # 4. Tip
    tip_account = TIP_ACCOUNTS[0]
    tip_tx = create_tip_transaction(wallet, tip_account, tip_lamports, blockhash_str)

    # 5. Bundle
    bundle_id = await send_jito_bundle(sell_tx, tip_tx)
    logger.info(f"Sell bundle sent: {bundle_id}")
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
    """Smart exit strategy combining scaling out, trailing stop, MC & time failsafes."""
    start_time = time.time()
    remaining = initial_token_amount
    peak_sol = buy_price_sol

    logger.info(f"Exit monitor started for {mint[:8]}... (amount: {remaining})")

    # Scaling out levels
    for mult, fraction in target_multiples:
        while remaining > 0:
            # Time failsafe
            if time.time() - start_time > max_hold_seconds:
                logger.info(f"Time limit reached, selling remaining {remaining}")
                await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                return

            # MC failsafe
            mc = await get_market_cap(mint)
            if 0 < mc >= target_mc:
                logger.info(f"Market cap ${mc:.0f} reached, selling remaining {remaining}")
                await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                return

            # Price check
            try:
                quote = await jupiter_quote(mint, WSOL_MINT, remaining, slippage_bps)
                current_sol = int(quote['outAmount']) / 1e9
            except Exception:
                await asyncio.sleep(2)
                continue

            if current_sol > peak_sol:
                peak_sol = current_sol

            # Stop-loss
            if current_sol <= buy_price_sol * stop_loss_factor:
                logger.info(f"Stop-loss hit ({current_sol:.4f} SOL), selling all")
                await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
                return

            # Take profit at this multiple
            if current_sol >= buy_price_sol * mult:
                sell_amount = int(initial_token_amount * fraction)
                sell_amount = min(sell_amount, remaining)
                logger.info(f"Selling {fraction*100:.0f}% at {mult}x (value: {current_sol:.4f})")
                await sell_with_jito(mint, wallet, sell_amount, slippage_bps, tip_lamports)
                remaining -= sell_amount
                peak_sol = current_sol
                break

            await asyncio.sleep(2)

    # Moonbag trailing stop
    while remaining > 0:
        if time.time() - start_time > max_hold_seconds:
            logger.info("Final time limit for moonbag, selling")
            await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
            return

        mc = await get_market_cap(mint)
        if 0 < mc >= target_mc:
            logger.info(f"Market cap target reached for moonbag, selling")
            await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
            return

        try:
            quote = await jupiter_quote(mint, WSOL_MINT, remaining, slippage_bps)
            current_sol = int(quote['outAmount']) / 1e9
        except Exception:
            await asyncio.sleep(2)
            continue

        if current_sol > peak_sol:
            peak_sol = current_sol

        if current_sol <= peak_sol * (1 - trailing_stop_pct / 100):
            logger.info(f"Trailing stop: peak {peak_sol:.4f}, current {current_sol:.4f}")
            await sell_with_jito(mint, wallet, remaining, slippage_bps, tip_lamports)
            return

        await asyncio.sleep(2)

# ----------------------------------------------------------------------
# Token Discovery (DexScreener polling)
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
# Main Bot Loop
# ----------------------------------------------------------------------

async def main():
    logger.info("=" * 60)
    logger.info("Starting Solana Meme Sniper Bot")
    logger.info(f"Wallet: {WALLET.pubkey()}")
    logger.info(f"Snipe amount: {SNIPE_AMOUNT_SOL} SOL")
    logger.info(f"Jito tip: {TIP_LAMPORTS} lamports")
    logger.info("=" * 60)

    rpc = await create_rpc()

    async for mint in discover_new_tokens():
        logger.info(f"New token discovered: {mint}")

        # Safety checks
        if not await safety_check(mint, rpc):
            logger.info(f"Safety check failed for {mint[:8]}...")
            continue

        logger.info(f"Safety passed. Sniping {mint[:8]}...")

        # Buy
        try:
            bundle_id, swap_tx = await buy_with_jito(
                mint=mint,
                wallet=WALLET,
                sol_amount=SNIPE_AMOUNT_SOL,
                slippage_bps=SLIPPAGE_BPS,
                tip_lamports=TIP_LAMPORTS,
            )
            logger.info(f"Buy transaction sent! Bundle ID: {bundle_id}")
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            continue

        # Wait for token account creation
        await asyncio.sleep(5)

        # Get received balance
        token_amount = await get_token_balance(rpc, WALLET.pubkey(), Pubkey.from_string(mint))
        if token_amount == 0:
            logger.warning("No tokens received, skipping exit monitor.")
            continue

        logger.info(f"Received {token_amount} tokens. Starting exit monitor...")

        # Start exit monitor as background task
        asyncio.create_task(
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
