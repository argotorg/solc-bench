"""Discover a contract's most popular calls and capture a fixture for
each one, plus a stub targets.toml for the group.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

import requests
import tomlkit

from solc_bench.add_benchmark.fixture_builder import (
    CURRENT_FORK_ACTIVATION_BLOCK,
    build_fixture_for_tx,
)

_ETHERSCAN_TIMEOUT = 30


def _parse_function_name(function_name: str) -> str | None:
    """The bare name from Etherscan's `functionName` field (e.g.
    "supply(address,...)" -> "supply"), or None if it's not decoded."""
    name = function_name.split("(", 1)[0].strip()
    return name.lower() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else None


def discover_popular_calls(
    address: str, api_key: str, limit: int, end_block: int | None = None
) -> tuple[Counter, dict[str, str], dict[str, str]]:
    """The `limit` most recent transactions to `address` up to `end_block`
    (default: latest), restricted to the current fork - selector counts,
    one example tx hash each, and a name from Etherscan's `functionName`."""
    address = address.lower()
    response = requests.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": 1,
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": end_block if end_block is not None else 99999999,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "apikey": api_key,
        },
        timeout=_ETHERSCAN_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "1" and body.get("message") != "No transactions found":
        raise RuntimeError(f"Etherscan txlist failed: {body.get('message')} - {body.get('result')}")

    counts: Counter = Counter()
    examples: dict[str, str] = {}
    names: dict[str, str] = {}
    for tx in body.get("result", []):
        if (tx.get("to") or "").lower() != address:
            continue
        if int(tx["blockNumber"]) < CURRENT_FORK_ACTIVATION_BLOCK:
            continue
        data = tx.get("input", "0x")
        if len(data) < 10:
            continue
        selector = data[:10].lower()
        counts[selector] += 1
        examples.setdefault(selector, tx["hash"])
        names.setdefault(selector, _parse_function_name(tx.get("functionName", "")) or "call")

    return counts, examples, names


def _current_block(rpc_url: str) -> int:
    response = requests.post(
        rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=_ETHERSCAN_TIMEOUT
    )
    response.raise_for_status()
    return int(response.json()["result"], 16)


def write_stub_targets_config(
    path: Path, address: str, discovery_address: str, discovery_end_block: int, discovery_limit: int
) -> None:
    """Write a targets.toml with `address`, `discovery_address` (the
    account calls were found on), and `discovery_end_block`/`discovery_limit`."""
    doc = tomlkit.document()
    entry = tomlkit.table()
    entry["address"] = address.lower()
    entry["discovery_address"] = discovery_address.lower()
    entry["discovery_end_block"] = discovery_end_block
    entry["discovery_limit"] = discovery_limit
    array = tomlkit.aot()
    array.append(entry)
    doc["target"] = array
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))


def _fixture_touches_address(fixture_path: Path, address: str) -> bool:
    """True if `address` appears (case-insensitively) in the fixture's pre-state -
    i.e. the transaction actually touched it, rather than just routing elsewhere."""
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    pre = data[test_name]["pre"]
    return address.lower() in {a.lower() for a in pre}


def capture_contract(
    address: str,
    rpc_url: str,
    etherscan_api_key: str,
    evmone_statetest_bin: Path,
    output_dir: Path,
    limit: int = 1000,
    max_selectors: int = 5,
    min_calls: int = 1,
    force: bool = False,
    end_block: int | None = None,
    target_address: str | None = None,
) -> list[Path]:
    """Capture one fixture per popular selector on `address` plus a stub
    targets.toml swapping `target_address` (default: `address` itself)."""
    end_block = end_block if end_block is not None else _current_block(rpc_url)
    counts, examples, names = discover_popular_calls(address, etherscan_api_key, limit, end_block)
    top = [(selector, n) for selector, n in counts.most_common(max_selectors) if n >= min_calls]
    if not top:
        raise ValueError(f"no calls to {address} found in the last {limit} transactions")

    swap_address = (target_address or address).lower()
    fixture_paths = []
    for selector, _ in top:
        name = names[selector]
        fixture_path = output_dir / f"{name}-{selector[2:]}.json"
        if fixture_path.exists() and not force:
            print(f"{fixture_path}: already exists, skipping", file=sys.stderr)
            continue
        try:
            build_fixture_for_tx(examples[selector], rpc_url, evmone_statetest_bin, fixture_path, test_name=name)
        except Exception as e:
            print(f"{examples[selector]}: failed to build, skipping ({e})", file=sys.stderr)
            if force and fixture_path.exists():
                # Stale fixture from a previous run at a different end_block/limit -
                # leaving it would misrepresent it as reproducible from this run's params.
                fixture_path.unlink()
            continue
        if not _fixture_touches_address(fixture_path, swap_address):
            # The target address is never even touched by this call - swapping
            # its bytecode in would be a no-op, so the fixture is useless for
            # `gas run`.
            print(f"{fixture_path}: target {swap_address} not in pre-state, removing", file=sys.stderr)
            fixture_path.unlink()
            continue
        fixture_paths.append(fixture_path)

    targets_toml = output_dir / "targets.toml"
    if not targets_toml.exists() or force:
        write_stub_targets_config(
            targets_toml, target_address or address, discovery_address=address,
            discovery_end_block=end_block, discovery_limit=limit,
        )

    return fixture_paths
