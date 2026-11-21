"""Run the toolkit action and shared client tests, each folder as its own discovery root."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("lib", "agents-toolkit-fetch", "agents-toolkit", "agents-census")

suite = unittest.TestSuite()
for folder in FOLDERS:
    directory = str(ROOT / folder)
    suite.addTests(unittest.defaultTestLoader.discover(directory, pattern="test_*.py", top_level_dir=directory))
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)
