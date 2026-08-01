"""Kalshi <-> Polymarket cross-venue arbitrage decision core.

The package is a functional core with an imperative shell. Everything under
`arb` except `arb.shell` is pure: no clock, no I/O, no randomness. Time and
connectivity arrive as events so that replay is deterministic.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
