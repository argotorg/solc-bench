"""Fixture-replay helper for gas benchmarking, used by the compile-time
benchmark suite (which reuses bytecode it already compiled instead of
compiling again) to replay a swapped-in fixture and collect its gas metrics.
"""

import json
import logging
import sys
import tempfile
from pathlib import Path

from solc_bench.statetest import fixture_replay

logger = logging.getLogger(__name__)


def run_replay_and_collect(fixture: dict, evmone, exempt_sender_balance: bool, log_label: str) -> dict:
    """Write `fixture` to a temp file, replay it, and return its gas metrics."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(fixture, f, indent=2)
        run_path = Path(f.name)
    try:
        result = fixture_replay(run_path, evmone, exempt_sender_balance)
    finally:
        run_path.unlink()

    gas_used = result.gas_used
    logger.debug(f"gasUsed({log_label})={gas_used}", file=sys.stderr)
    for note in result.notes:
        logger.debug("note: %s", note)
    return {"gas_used": {"values": [gas_used], "median": gas_used, "mean": gas_used}}
