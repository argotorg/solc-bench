"""Fixture-replay helper for gas benchmarking, used by the compile-time
benchmark suite (which reuses bytecode it already compiled instead of
compiling again) to replay a swapped-in fixture and collect its gas metrics.
"""

import json
import sys
import tempfile
from pathlib import Path

from solc_bench.statetest import fixture_replay


def run_replay_and_collect(fixture: dict, evmone_statetest, exempt_sender_balance: bool, log_label: str) -> dict:
    """Write `fixture` to a temp file, replay it, and return its gas metrics."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(fixture, f, indent=2)
        run_path = Path(f.name)
    try:
        result = fixture_replay(run_path, evmone_statetest, exempt_sender_balance)
    finally:
        run_path.unlink()

    gas_used = result.gas_used
    print(f"gasUsed({log_label})={gas_used}", file=sys.stderr)
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return {"gas_used": {"values": [gas_used], "median": gas_used, "mean": gas_used}}
