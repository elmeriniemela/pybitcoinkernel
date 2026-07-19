import sys
from pathlib import Path

# Make tests/util.py importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent))
