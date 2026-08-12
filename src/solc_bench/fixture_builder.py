"""Build GeneralStateTest fixtures that replay real mainnet transactions.

The fixture format itself (`env`/`pre`/`transaction`/`post`) is not
evmone-specific - it's the standard cross-client state test format defined
by the Ethereum execution-specs / execution-spec-tests (EEST) project
(https://github.com/ethereum/execution-specs), the same format geth, besu,
nethermind, reth, etc. all consume for consensus testing. `evmone-statetest`
is just one client-side runner/loader for it, same as any other client's.
Only the `requiredStatus`/`requiredStateDiff` fields this module adds to
`post.<fork>[0]` (see `build_traced_replay_fixture`) are our own extension on
top - not part of the standard spec, and other loaders would just ignore
them as unknown JSON.

This is a small counterpart to purplebench
(https://github.com/DanielVF/purplebench): instead of replaying a captured
mainnet transaction through revm, it drives evmone-statetest directly - the
target transaction's full traced pre-state (see `fetch_prestate`) is fetched
straight from a trace-capable RPC endpoint and assembled into a fixture (see
`build_traced_replay_fixture`).

The fixture's expected `logs` hash is computed directly from the real mined
receipt (see `fetch_required_logs_hash`) - unlike the state root, this needs
no help from evmone, so a plain `evmone-statetest` run against the finished
fixture is already a genuine correctness check that evmone's logs match
mainnet's, not just a self-consistency one. The state root genuinely has no
independent source (our fixture's `pre`/`post` is a synthetic view of only
the touched accounts, not the full chain state a real root commits to), so
evmone itself is still used to compute it (see `solve_expected_hashes`).

`build_fixture_for_tx` is the intended entry point - also exposed as
`solc-bench capture-tx` - since fixtures are meant to be captured rarely and
then committed, not regenerated on every run.

Fixtures built here also carry independent mainnet ground truth (the real
transaction's execution status and account/storage diff) alongside the
ordinary `hash`/`logs`/`indexes` fields in each case's `post.<fork>[0]`
object; see `statetest.py` for running a built fixture through
evmone-statetest and verifying its output against that ground truth.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

FORK = "Prague"  # current mainnet fork; must be >= Prague for EIP-7702-delegated accounts

_RPC_TIMEOUT = 15
_TRACE_RPC_TIMEOUT = 60


def _rpc(url: str, method: str, params: list[Any]) -> Any:
    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=_RPC_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"{method} failed: {body['error']}")
    return body["result"]


def _hex(n: int) -> str:
    return hex(n)


def _placeholder_post(logs_hash: str | None = None) -> dict[str, Any]:
    """The `post` block. `hash` (state root) is always a placeholder (see
    `solve_expected_hashes`) - there's no independent source for it. `logs`
    defaults to a placeholder too, but if `logs_hash` is given (see
    `fetch_required_logs_hash`), the real mainnet value is used directly
    instead - `solve_expected_hashes` then treats any evmone-computed
    mismatch against it as a genuine error rather than something to recover
    and patch over.

    `requiredStatus`/`requiredStateDiff` aren't set up front here - they're
    attached later, in place, by `patch_required_ground_truth`. Unlike
    `hash`/`logs`, evmone-statetest's loader never reads them (they're inert
    extra JSON, only meaningful to `statetest.verify_replay`), so there's no
    need to have them ready before the fixture is written or before evmone
    runs against it.
    """
    return {
        FORK: [
            {
                "hash": "0x0",
                "logs": logs_hash if logs_hash is not None else "0x0",
                "indexes": {"data": 0, "gas": 0, "value": 0},
            }
        ]
    }


def _env_from_block(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "currentCoinbase": block["miner"],
        "currentGasLimit": block["gasLimit"],
        "currentNumber": block["number"],
        "currentTimestamp": block["timestamp"],
        "currentRandom": block["mixHash"],
        "currentBaseFee": block["baseFeePerGas"],
        "currentExcessBlobGas": "0x0",
    }


def _tx_from_mainnet(tx: dict[str, Any]) -> dict[str, Any]:
    """Build a TestMultiTransaction entry directly from an `eth_getTransactionByHash` result.

    Only `sender` is used to attribute the caller - evmone's loader never reads
    r/s/v for this multi-tx shape, so no real signature is required to replay
    someone else's mainnet transaction.
    """
    out: dict[str, Any] = {
        "nonce": tx["nonce"],
        "gasLimit": [tx["gas"]],
        "to": tx["to"],
        "value": [tx["value"]],
        "data": [tx["input"]],
        "sender": tx["from"],
    }
    if tx.get("type") == "0x0":
        out["gasPrice"] = tx["gasPrice"]
    else:
        out["maxFeePerGas"] = tx["maxFeePerGas"]
        out["maxPriorityFeePerGas"] = tx["maxPriorityFeePerGas"]
    if tx.get("accessList"):
        out["accessLists"] = [tx["accessList"]]
    return out


def fetch_prestate(tx_hash: str, rpc_url: str) -> dict[str, dict[str, Any]]:
    """Every account touched anywhere in the transaction's call graph, as it was
    immediately before execution, via `debug_traceTransaction`'s `prestateTracer`.

    Needs a trace-capable RPC endpoint - most free public nodes don't expose
    `debug_traceTransaction` at all. Returns the raw per-address tracer
    output, keyed by address, with whatever subset of balance/nonce/code/
    storage each account happened to touch.
    """
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "method": "debug_traceTransaction",
            "params": [tx_hash, {"tracer": "prestateTracer"}],
            "id": 1,
        },
        timeout=_TRACE_RPC_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"debug_traceTransaction failed: {body['error']}")
    return body["result"]


def _normalize_prestate_account(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill in evmone's required TestState fields and drop zero-value storage
    entries (evmone's `validate_state` rejects explicit zero-value storage)."""
    nonce = raw.get("nonce", 0)
    if isinstance(nonce, int):
        nonce = hex(nonce)
    storage = {k: v for k, v in raw.get("storage", {}).items() if int(v, 16) != 0}
    return {
        "nonce": nonce,
        "balance": raw.get("balance", "0x0"),
        "code": raw.get("code", "0x"),
        "storage": storage,
    }


def fetch_required_status(tx_hash: str, rpc_url: str) -> str:
    """The real transaction's outcome (`"0x1"` success / `"0x0"` failure), from
    its mined receipt. Embedded in a fixture's `post.<fork>[0].requiredStatus`
    (see `_placeholder_post`) for `statetest.verify_replay` to check evmone's
    own execution status against - independent ground truth that the
    replay's overall outcome matches mainnet, not just an isolated
    `EVMC_SUCCESS` check on evmone's side alone.
    """
    receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
    return receipt["status"]


def fetch_required_state_diff(tx_hash: str, rpc_url: str) -> dict[str, Any]:
    """The real chain's own account/storage diff for this transaction, via
    `prestateTracer`'s `diffMode`, normalized into the same
    `{"modifiedAccounts": {...}, "deletedAccounts": [...]}` shape evmone's own
    `to_json(state::StateDiff)` (see `--dump-statediff`) produces, so the two
    are directly comparable by `statetest.verify_replay`.

    This is the "required" (ground-truth) diff embedded in a fixture's
    `post.<fork>[0].requiredStateDiff` (see `_placeholder_post`) - independent
    evidence a replayed transaction's computed post-state is actually
    correct, not just internally consistent with itself (see
    `solve_expected_hashes`, which only checks the latter).

    One documented quirk to know about (see `statetest.verify_replay`): geth's
    own diffMode implementation (`eth/tracers/native/prestate.go`) deliberately
    omits a storage slot from `post` when its final value is exactly zero -
    "don't include the empty slot" - even though the account itself is still
    marked modified. A slot cleared to 0 will therefore never appear here,
    even when it really changed.
    """
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "method": "debug_traceTransaction",
            "params": [tx_hash, {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}}],
            "id": 1,
        },
        timeout=_TRACE_RPC_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"debug_traceTransaction (diffMode) failed: {body['error']}")
    result = body["result"]
    return _normalize_required_state_diff(result["pre"], result["post"])


def _normalize_required_state_diff(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    modified_accounts = {}
    for addr, delta in post.items():
        pre_acc = pre.get(addr, {})
        acc: dict[str, Any] = {
            "nonce": _hex(delta.get("nonce", pre_acc.get("nonce", 0))),
            "balance": delta.get("balance", pre_acc.get("balance", "0x0")),
        }
        if "code" in delta:
            acc["code"] = delta["code"]
        acc["modifiedStorage"] = dict(delta.get("storage", {}))
        modified_accounts[addr] = acc

    # Accounts present in `pre` but dropped from `post` entirely were destroyed
    # (see prestate.go: "The deleted account's state is pruned from `post`").
    deleted_accounts = [addr for addr in pre if addr not in post]

    return {"modifiedAccounts": modified_accounts, "deletedAccounts": deleted_accounts}


def _rlp_length_prefix(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    length_bytes = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(length_bytes)]) + length_bytes


def _rlp_encode_bytes(b: bytes) -> bytes:
    if len(b) == 1 and b[0] < 0x80:
        return b
    return _rlp_length_prefix(len(b), 0x80) + b


def _rlp_encode_list(items: list[bytes]) -> bytes:
    payload = b"".join(items)
    return _rlp_length_prefix(len(payload), 0xC0) + payload


def _keccak256(data: bytes) -> str:
    """Shell out to `cast keccak` rather than adding a Keccak-256 dependency -
    Python's `hashlib.sha3_256` is the NIST SHA3 finalist, a different
    algorithm from what Ethereum actually uses (different padding)."""
    result = subprocess.run(
        ["cast", "keccak", "0x" + data.hex()],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def compute_logs_hash(logs: list[dict[str, Any]]) -> str:
    """`keccak256(rlp::encode(logs))` - matches evmone's `logs_hash()`
    (`test/utils/statetest_logs_hash.cpp`) exactly: each log RLP-encodes as
    `[address, [topics...], data]` (`test/utils/rlp_encode.cpp`'s
    `rlp_encode(const Log&)`), and the whole list is RLP-wrapped.

    `logs` is `eth_getTransactionReceipt(tx)["logs"]`'s raw entries.
    """
    encoded_logs = []
    for log in logs:
        address = bytes.fromhex(log["address"][2:])
        topics = [bytes.fromhex(t[2:]) for t in log["topics"]]
        data = bytes.fromhex(log["data"][2:])
        encoded_logs.append(
            _rlp_encode_list(
                [
                    _rlp_encode_bytes(address),
                    _rlp_encode_list([_rlp_encode_bytes(t) for t in topics]),
                    _rlp_encode_bytes(data),
                ]
            )
        )
    return _keccak256(_rlp_encode_list(encoded_logs))


def fetch_required_logs_hash(tx_hash: str, rpc_url: str) -> str:
    """The real transaction's `logs_hash` (`keccak256(rlp(logs))`), computed
    directly from its mined receipt's `logs`. Unlike the state root, this
    needs no help from evmone - it's written straight into the fixture's
    `post.<fork>[0].logs` (see `build_traced_replay_fixture`) in place of a
    placeholder, so a plain `evmone-statetest` run is already a genuine
    correctness check that evmone's logs match mainnet's, not merely a
    self-consistency check.
    """
    receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
    return compute_logs_hash(receipt["logs"])


def build_traced_replay_fixture(
    test_name: str,
    tx: dict[str, Any],
    block: dict[str, Any],
    prestate: dict[str, dict[str, Any]],
    logs_hash: str | None = None,
) -> dict[str, Any]:
    """Assemble a state test from a real transaction using its full traced pre-state.

    `prestate` is expected to be `fetch_prestate`'s output, so every contract
    the transaction touched anywhere - routers, hooks, tokens, pools, the
    coinbase - is already present with its real pre-execution code/balance/
    nonce/storage, correctly scoped to the transaction's actual position
    within its block (not just its block - see `fetch_prestate`).

    `logs_hash`, if given (see `fetch_required_logs_hash`), is used directly
    as the case's expected `logs` value instead of a placeholder - see
    `_placeholder_post`.

    Ground truth (`requiredStatus`/`requiredStateDiff`) isn't attached here -
    see `patch_required_ground_truth`, meant to run afterward.
    """
    pre = {addr: _normalize_prestate_account(acc) for addr, acc in prestate.items()}
    return {
        test_name: {
            "env": _env_from_block(block),
            "pre": pre,
            "transaction": _tx_from_mainnet(tx),
            "post": _placeholder_post(logs_hash),
            "_info": {"hash": tx["hash"], "chainId": tx.get("chainId", "0x1")},
        }
    }


def build_fixture_for_tx(
    tx_hash: str,
    rpc_url: str,
    evmone_statetest_bin: Path,
    output_path: Path,
    test_name: str | None = None,
) -> Path:
    """One-shot: build a complete, ready-to-commit fixture for `tx_hash` and
    write it to `output_path`.

    This is the intended entry point for capturing a new fixture (also
    exposed as `solc-bench capture-tx`) - fixtures are meant to be built
    rarely and then committed, not regenerated on every run, so everything
    `build_traced_replay_fixture` needs (prestate), finalizing the hash/logs
    fields (`solve_expected_hashes`/`patch_expected_hashes`), and attaching
    ground truth (`patch_required_ground_truth`) happens here in one call.
    `rpc_url` must be a trace-capable endpoint (see `fetch_prestate`); most
    free public nodes don't expose `debug_traceTransaction`.

    `test_name` defaults to `tx-<first 10 hex chars of tx_hash>`.
    """
    tx = _rpc(rpc_url, "eth_getTransactionByHash", [tx_hash])
    if tx is None:
        raise ValueError(f"transaction not found: {tx_hash}")
    block = _rpc(rpc_url, "eth_getBlockByNumber", [tx["blockNumber"], False])
    prestate = fetch_prestate(tx_hash, rpc_url)
    required_diff = fetch_required_state_diff(tx_hash, rpc_url)
    required_status = fetch_required_status(tx_hash, rpc_url)
    logs_hash = fetch_required_logs_hash(tx_hash, rpc_url)

    name = test_name or f"tx-{tx_hash[2:12]}"
    fixture = build_traced_replay_fixture(name, tx, block, prestate, logs_hash)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(fixture, indent=2) + "\n")

    hashes = solve_expected_hashes(output_path, evmone_statetest_bin)
    patch_expected_hashes(output_path, hashes)
    patch_required_ground_truth(output_path, required_status, required_diff)
    return output_path


_MISMATCH_RE = re.compile(
    r"Expected equality of these values:\n"
    r"\s*(?P<actual_expr>\S+)\n"
    r"\s*Which is: (?P<actual>0x[0-9a-fA-F]+)\n"
    r"\s*(?P<expected_expr>\S+)\n"
    r"\s*Which is: (?P<expected>0x[0-9a-fA-F]+)"
)


def solve_expected_hashes(fixture_path: Path, evmone_statetest_bin: Path) -> dict[str, str]:
    """Run evmone-statetest against a placeholder state root and recover the
    actual value.

    evmone-statetest asserts `state_root == expected.state_hash` with gtest's
    EXPECT_EQ, which prints both sides on mismatch. Since we seed the fixture
    with a `"0x0"` placeholder, the run "fails" but the failure message hands
    us the real, evmone-computed value instead of us re-implementing MPT
    hashing in Python.

    `logs` is handled differently depending on whether the fixture already
    has a real value there (see `fetch_required_logs_hash`,
    `build_traced_replay_fixture`) or is still a `"0x0"` placeholder:
      - Real value already present: expected to already match, so no mismatch
        should be found. If evmone computes something different, that's a
        genuine correctness problem - raised immediately, not silently
        patched over.
      - Still a placeholder: recovered the same way as the state root (the
        old, pre-`fetch_required_logs_hash` behavior).
    """
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    logs_prefilled = data[test_name]["post"][FORK][0]["logs"] != "0x0"

    result = subprocess.run(
        [str(evmone_statetest_bin), str(fixture_path), "--gtest_filter=*", "--gtest_print_time=0"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    matches = list(_MISMATCH_RE.finditer(output))
    hashes: dict[str, str] = {}
    for m in matches:
        if m.group("actual_expr") == "state_root":
            hashes["hash"] = m.group("actual")
        elif "logs_hash" in m.group("actual_expr"):
            hashes["logs"] = m.group("actual")

    if "hash" not in hashes:
        raise RuntimeError(f"could not recover state root from evmone-statetest output:\n{output}")

    if logs_prefilled:
        if "logs" in hashes:
            raise RuntimeError(
                f"{fixture_path}: evmone computed a different logs hash "
                f"({hashes['logs']}) than the real mainnet value already in "
                "the fixture - evmone's execution disagrees with the real "
                f"chain's logs:\n{output}"
            )
    elif "logs" not in hashes:
        raise RuntimeError(f"could not recover logs hash from evmone-statetest output:\n{output}")

    return hashes


def patch_expected_hashes(fixture_path: Path, hashes: dict[str, str]) -> None:
    """Write `hashes` (see `solve_expected_hashes`) back into the fixture.
    `logs` is only present in `hashes` when it was recovered from a
    placeholder - when a real value was pre-filled and already matched,
    there's nothing to overwrite."""
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    case = data[test_name]["post"][FORK][0]
    case["hash"] = hashes["hash"]
    if "logs" in hashes:
        case["logs"] = hashes["logs"]
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")


def patch_required_ground_truth(
    fixture_path: Path,
    required_status: str | None,
    required_state_diff: dict[str, Any] | None,
) -> None:
    """Attach ground truth (see `fetch_required_status`/`fetch_required_state_diff`)
    to a fixture's existing `post.<fork>[0]` case, in place, for
    `statetest.verify_replay` to check evmone's actual output against.

    Independent of `solve_expected_hashes`/`patch_expected_hashes`: unlike
    `hash`/`logs`, evmone-statetest's loader never reads these fields, so
    there's no need to run this before or alongside finalizing the fixture -
    it can run any time after the fixture already exists on disk.
    """
    if required_status is None and required_state_diff is None:
        return
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    case = data[test_name]["post"][FORK][0]
    if required_status is not None:
        case["requiredStatus"] = required_status
    if required_state_diff is not None:
        case["requiredStateDiff"] = required_state_diff
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")
