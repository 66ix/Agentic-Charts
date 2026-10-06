import os

# Tests share one app instance and one client address; per-client rate limits would make them order-dependent.
os.environ.setdefault("AGENT_RATE_LIMIT", "0")
os.environ.setdefault("API_RATE_LIMIT", "0")
