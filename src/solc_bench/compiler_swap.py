"""Measure how a candidate solc build changes gas usage for a real mainnet
transaction, by compiling the target contract's source and swapping the
freshly-compiled runtime bytecode into an already-captured fixture (see
`fixture_builder.py`) before replaying it through evmone-statetest - the same
"swap freshly-compiled code into a real recorded transaction" technique
purplebench (https://github.com/DanielVF/purplebench) uses, just against our
own fixtures instead of revm.

Typical flow (see `compare_gas` for all four tied together):
  1. `fetch_and_cache_source` - fetch the contract's real, verified source
     fresh from Sourcify (see `sourcify.fetch_contract_source`) and cache it
     in a file named by the contract's own mainnet address, so the cache is
     unambiguous regardless of what human-readable name a repo file might
     otherwise have. A `benchmark_data/<key>.json` standard-json input (per
     CLAUDE.md) can be passed instead to skip this - see `compare_gas`.
  2. `compile_deployed_bytecode` - compile that source with a candidate solc
     build.
  3. `swap_contract_code` - write a copy of an existing fixture with that
     contract's `pre[address].code` replaced by the freshly-compiled bytecode.
  4. `statetest.verify_replay(..., exempt_sender_balance=True)` - run the
     swapped fixture through evmone-statetest and check it against the
     fixture's own ground truth: the candidate build must still succeed/fail
     exactly like mainnet, emit exactly the same logs, and produce exactly
     the same StateDiff except the sender's `balance` (gas cost is expected
     to differ; nothing else about the transaction's real effects should).
     Only the state root (`post.<fork>[0].hash`) is never compared - it
     directly encodes the account's `code` field, so it necessarily changes
     whenever the bytecode does, even when nothing else did.

Fetching real, verified source instead of relying on a hand-maintained
standard-json file has a real tradeoff, worth knowing about: Sourcify's
source is whatever was actually verified for that address, at whatever
Solidity syntax era that was. For an old contract (WETH9, deployed 2017,
verified as solc 0.4.19) that source uses syntax modern solc no longer
parses at all (e.g. the pre-0.6 anonymous `function() public payable {}`
fallback) - relaxing the pragma (see `sourcify.fetch_contract_source`)
doesn't fix that, since the syntax itself, not just the version pin, is
incompatible. `compile_deployed_bytecode` surfaces this as an ordinary
compile error; there's no workaround here beyond picking compilers old
enough to accept the source, or passing a hand-maintained/modernized
standard-json input instead (which is what `benchmark_data/weth9.json` is:
a rewritten, non-authentic WETH9 source that compiles with modern solc).

Known limitation: contracts with Solidity immutables set by their
constructor (not the case for WETH9, this module's first user) would need
their immutable references patched into the freshly-compiled bytecode after
compiling and before swapping - recompiling from scratch produces zero-filled
placeholders in their place, same issue purplebench's own README documents
for its own immutable patching. Not implemented here.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from solc_bench.sourcify import fetch_contract_source
from solc_bench.statetest import ReplayMismatch, verify_replay


def fetch_and_cache_source(address: str, cache_dir: Path) -> tuple[Path, str, str]:
    """Fetch `address`'s verified source from Sourcify (see
    `sourcify.fetch_contract_source`) and cache it at
    `cache_dir/<address>.json`, keyed by the contract's own mainnet address
    (its proxy implementation's, if it has one) rather than any human-chosen
    name, so the cache is unambiguous and directly re-derivable from the
    address alone.

    Returns `(path, source_name, contract_name)` - the latter two are what
    `compile_deployed_bytecode` needs alongside the path.
    """
    result = fetch_contract_source(address)
    cache_address = (result.implementation_address or address).lower()
    path = cache_dir / f"{cache_address}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.standard_json, indent=2) + "\n")
    return path, result.source_name, result.contract_name


def compile_deployed_bytecode(
    solc_bin: str | Path,
    standard_json_path: Path,
    contract_name: str,
    source_name: str | None = None,
) -> str:
    """Compile `standard_json_path` with `solc_bin --standard-json` and return
    the runtime (deployed) bytecode for `contract_name`, as `"0x..."`.

    `source_name` selects which source file's `contract_name` to read; only
    needed if the standard-json input declares more than one source file.
    Requires `settings.outputSelection` to already include
    `evm.deployedBytecode.object` for the contract - already true for
    `benchmark_data/*.json` (see CLAUDE.md).
    """
    with open(standard_json_path, encoding="utf-8") as f:
        result = subprocess.run(
            [str(solc_bin), "--standard-json"],
            stdin=f,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(f"{solc_bin} exited {result.returncode}:\n{result.stderr}")

    output = json.loads(result.stdout)
    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    if errors:
        messages = "\n".join(e.get("formattedMessage", e.get("message", "")) for e in errors)
        raise RuntimeError(f"{standard_json_path} failed to compile with {solc_bin}:\n{messages}")

    contracts = output.get("contracts", {})
    if source_name is None:
        if len(contracts) != 1:
            raise ValueError(
                f"{standard_json_path} has {len(contracts)} source file(s); pass "
                f"source_name explicitly (one of {sorted(contracts)})"
            )
        (source_name,) = contracts.keys()

    try:
        contract = contracts[source_name][contract_name]
    except KeyError as e:
        available = sorted(contracts.get(source_name, {}))
        raise ValueError(
            f"{contract_name!r} not found in {source_name!r} (available: {available})"
        ) from e

    runtime = contract["evm"]["deployedBytecode"]["object"]
    if not runtime:
        raise ValueError(f"{contract_name} in {source_name} has no deployed bytecode")
    return f"0x{runtime}"


def swap_contract_code(
    fixture_path: Path,
    address: str,
    new_code: str,
    output_path: Path,
) -> Path:
    """Write a copy of `fixture_path` with `pre[address].code` replaced by
    `new_code`, for comparison against a candidate compiler build.

    `requiredStatus`/`requiredStateDiff`/`logs` are kept as-is in the copy,
    deliberately: they're still exactly what the candidate build's replay is
    expected to reproduce (see `statetest.verify_replay`'s
    `exempt_sender_balance` for the one legitimate exception - the sender
    pays whatever gas the new bytecode actually costs). Only the state root
    (`post.<fork>[0].hash`) is expected to change, since it directly encodes
    the account's `code` field - `verify_replay` never checks it.
    """
    address = address.lower()
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    if address not in data[test_name]["pre"]:
        raise ValueError(f"{fixture_path}: {address} not found in pre-state")

    data[test_name]["pre"][address]["code"] = new_code

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n")
    return output_path


def compare_gas(
    fixture_path: Path,
    address: str,
    baseline_solc: str | Path,
    candidate_solc: str | Path,
    evmone_statetest_bin: Path,
    work_dir: Path,
    standard_json_path: Path | None = None,
    contract_name: str | None = None,
    source_name: str | None = None,
) -> dict[str, Any]:
    """Compile `contract_name` with both `baseline_solc` and `candidate_solc`,
    swap each build into its own copy of `fixture_path` at `address`, verify
    each swapped replay through evmone-statetest (see
    `statetest.verify_replay`, called with `exempt_sender_balance=True`), and
    return the comparison.

    Each build's result includes `pass`/`gas_used` (from `--trace-summary`),
    `notes` (informational, see `verify_replay`), and `mismatch`: `None` if
    the build's replay matched the fixture's ground truth (status, logs,
    and StateDiff other than the sender's gas cost), or the mismatch message
    otherwise. A mismatch does *not* stop the comparison - both builds are
    still compiled, swapped, and measured, and `gas_used`/`pass` are still
    populated for a mismatching build (see `statetest.ReplayMismatch.result`)
    - a build behaving differently from mainnet is itself a real, worth
    reporting finding, not a reason to abort the whole comparison.

    If `standard_json_path`/`contract_name` aren't given, the contract's
    source is fetched fresh from Sourcify and cached under
    `work_dir/sources/` (see `fetch_and_cache_source`) - note the tradeoff
    documented at the top of this module: real source, but not guaranteed to
    compile with every candidate solc version. Pass `standard_json_path`
    explicitly (e.g. a `benchmark_data/*.json`) to sidestep that.

    `address` must match whichever address in the fixture's `pre` actually
    holds the logic bytecode to replace - for a proxy contract, that's the
    *implementation* address, not the proxy's own (Sourcify's own address
    resolves the same way, but the fixture's `pre` doesn't know it's a
    proxy at all, so this isn't done automatically here).
    """
    if standard_json_path is None or contract_name is None:
        standard_json_path, source_name, contract_name = fetch_and_cache_source(
            address, work_dir / "sources"
        )

    results: dict[str, dict[str, Any]] = {}
    for label, solc_bin in (("baseline", baseline_solc), ("candidate", candidate_solc)):
        code = compile_deployed_bytecode(solc_bin, standard_json_path, contract_name, source_name)
        swapped_path = work_dir / f"{fixture_path.stem}.{label}.json"
        swap_contract_code(fixture_path, address, code, swapped_path)
        try:
            replay = verify_replay(swapped_path, evmone_statetest_bin, exempt_sender_balance=True)
            mismatch = None
        except ReplayMismatch as e:
            replay = e.result
            mismatch = str(e)
        results[label] = {
            "pass": replay.passed,
            "gas_used": replay.gas_used,
            "code": code,
            "notes": replay.notes,
            "mismatch": mismatch,
        }

    baseline_gas = results["baseline"]["gas_used"]
    candidate_gas = results["candidate"]["gas_used"]
    delta = None
    delta_pct = None
    if baseline_gas is not None and candidate_gas is not None:
        delta = candidate_gas - baseline_gas
        if baseline_gas != 0:
            delta_pct = 100.0 * delta / baseline_gas

    return {
        "baseline": results["baseline"],
        "candidate": results["candidate"],
        "gas_delta": delta,
        "gas_delta_pct": delta_pct,
    }
