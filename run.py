#!/usr/bin/env python3
"""Runner script — ensures correct sys.path for module imports."""

import os
import sys

# Add repo root to sys.path so `src` is importable from any working directory
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Also set cwd to repo root for data/ directory access
os.chdir(REPO_ROOT)

from legacy_code.main import main

if __name__ == "__main__":
    main()
