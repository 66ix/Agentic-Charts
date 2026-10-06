"""Trick's Kimi Cooked - Elite Edition v5.7.4, ported from Pine Script to Python by its author.

`kimi_v574.py` is the port as delivered, plus a few additive lines marked `agentic-charts:` that keep the
label positions, the candle that broke each S/R level, the zone width and the Fib swing for drawing.
None of them change a computed value. PORT_README.md is the port's own documentation.
"""

from .kimi_v574 import SIG_NAMES, Inputs, KimiCooked, Result, Signal, __version__  # noqa: F401
