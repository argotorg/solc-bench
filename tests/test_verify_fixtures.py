"""Replay every gas-bench fixture in benchmark_data/gas against evmone and
verify it matches the mainnet ground truth captured in it."""

from pathlib import Path

import pytest

from solc_bench.statetest import ReplayMismatch, fixture_replay

REPO = Path(__file__).resolve().parent.parent
GAS_DIR = REPO / "benchmark_data" / "gas"

FIXTURES = sorted(GAS_DIR.glob("*/*.json"))


@pytest.mark.parametrize(
    "fixture_path", FIXTURES, ids=[str(p.relative_to(GAS_DIR)) for p in FIXTURES]
)
def test_gas_fixture_replay(evmone, fixture_path):
    try:
        fixture_replay(fixture_path, evmone)
    except ReplayMismatch as e:
        pytest.fail(str(e))
