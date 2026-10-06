"""Finding the coin a request is about ("show me ETH", "same on solana", "SOLUSDT daily")."""

from __future__ import annotations

import re

NAMES: dict[str, str] = {
    "bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "ripple": "XRP", "dogecoin": "DOGE",
    "cardano": "ADA", "avalanche": "AVAX", "chainlink": "LINK", "polkadot": "DOT", "injective": "INJ",
    "litecoin": "LTC", "toncoin": "TON", "aptos": "APT", "arbitrum": "ARB", "optimism": "OP", "celestia": "TIA",
    "binance coin": "BNB", "near protocol": "NEAR", "pepe": "PEPE", "shiba": "SHIB", "polygon": "POL",
    "sui": "SUI", "sei": "SEI", "tron": "TRX", "stellar": "XLM",
    "uniswap": "UNI", "aave": "AAVE", "hyperliquid": "HYPE", "bonk": "BONK", "wif": "WIF", "jupiter": "JUP",
}
# Tickers that are also everyday words: only matched when typed in capitals ("NEAR", not "near support").
AMBIGUOUS = {"NEAR", "OP", "TON", "DOT", "LINK", "ONE", "GAS", "SUN", "MOVE", "ME", "IO", "BAN", "T", "S", "A",
             "AI", "ANY", "FUN", "MASK", "ID", "HIGH", "LOW", "BOND", "GO", "UP", "PEOPLE", "SAFE", "POND", "FET",
             "RENDER", "OM", "AUCTION", "ACT", "USUAL", "TRUMP", "BIO", "PROM", "HOOK", "KEY", "FIS", "ARK", "DATA",
             "WIN", "DENT", "ORDER", "BEL", "COS", "DEGO", "LIT", "ALT", "BLUR", "MAGIC", "STRK", "DYDX", "PUNDIX",
             "CHESS", "ZRO", "NOT", "IN", "EDU", "COW", "CAT", "ETC", "HYPE", "APT", "ATOM", "RUNE", "UNI"}
COMMON = {"BTC", "ETH", "SOL", "BNB", "XRP", "INJ", "DOGE", "ADA", "AVAX", "DOT", "TON", "SUI", "APT", "ARB", "OP",
          "NEAR", "TIA", "SEI", "LTC", "LINK", "PEPE", "SHIB", "TRX", "XLM", "UNI", "AAVE", "WIF", "BONK", "JUP",
          "ENA", "FET", "RENDER", "POL", "HBAR", "ATOM", "FIL", "ETC", "BCH", "ICP", "STX", "IMX", "RUNE", "HYPE"}
QUOTES = ("USDT", "USDC", "FDUSD")


def find_symbol(text: str, known_bases: set[str] | None = None) -> str | None:
    """The first coin mentioned in `text`, as a USDT pair, or None."""
    bases = COMMON | (known_bases or set())
    m = re.search(r"\b([A-Za-z0-9]{2,12})(?:/|-)?(USDT|USDC|FDUSD)\b", text, flags=re.I)
    if m and m.group(1).upper() not in ("THE", "ON", "IN"):
        return f"{m.group(1).upper()}{m.group(2).upper()}"
    low = text.lower()
    for name in sorted(NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return f"{NAMES[name]}USDT"
    for tok in re.finditer(r"\$?\b([A-Za-z0-9]{2,10})\b", text):
        word = tok.group(1)
        up = word.upper()
        if up not in bases:
            continue
        if up in AMBIGUOUS and not (word.isupper() or tok.group(0).startswith("$")):
            continue
        return f"{up}USDT"
    return None


def base_of(symbol: str) -> str:
    for q in QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol
