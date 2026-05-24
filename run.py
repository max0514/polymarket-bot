#!/usr/bin/env python3
"""Run the live Polymarket crypto order book data server."""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

os.chdir(REPO_ROOT)

from live_btc_orderbook_data_server import main

if __name__ == "__main__":
    main()
