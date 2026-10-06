import os

# Keep tests away from the real candle cache in backend/.cache: a warm cache would change which
# requests the mocked-Binance tests see. test_candle_cache.py turns it back on with a tmp path.
os.environ["CANDLE_CACHE"] = "off"
