"""Build state-test fixtures (https://github.com/ethereum/execution-spec-tests)
that replay real mainnet transactions.

`build_fixture_for_tx` is the entry point, used by `solc-bench capture-contract`.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from solc_bench.statetest import ReplayMismatch, fixture_replay

_RPC_TIMEOUT = 15
_TRACE_RPC_TIMEOUT = 60

# Mainnet fork activation blocks, oldest first. A transaction is executed
# under whichever fork was actually live at its block, so replay uses the
# same rules (e.g. EIP-7623's calldata floor) the real chain did.
_FORK_ACTIVATION_BLOCKS = [
    (12965000, "London"),
    (15537394, "Paris"),
    (17034870, "Shanghai"),
    (19426587, "Cancun"),
    (22431084, "Prague"),
]

# Older transactions are skipped at discovery (capture_contract.py): a
# modern solc build can emit opcodes an older fork doesn't support.
CURRENT_FORK_ACTIVATION_BLOCK = _FORK_ACTIVATION_BLOCKS[-1][0]


def _fork_for_block(block_number: int) -> str:
    fork = _FORK_ACTIVATION_BLOCKS[0][1]
    for activation_block, name in _FORK_ACTIVATION_BLOCKS:
        if block_number >= activation_block:
            fork = name
    return fork


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


def _placeholder_post(fork: str, logs_hash: str) -> dict[str, Any]:
    """The `post` block. `hash` (state root) is a placeholder to be
    resolved later - see `_solve_expected_state_root`."""
    return {
        fork: [
            {
                "hash": "0x0",
                "logs": logs_hash,
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
    """Build a transaction entry from an `eth_getTransactionByHash` result."""
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


def _fetch_prestate(tx_hash: str, rpc_url: str) -> dict[str, dict[str, Any]]:
    """Every account touched by the transaction, as it was immediately
    before execution. Needs a trace-capable RPC endpoint."""
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
    """Fill in required fields and drop zero-value storage entries."""
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


def _fetch_receipt(tx_hash: str, rpc_url: str) -> dict[str, Any]:
    return _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])


def _fetch_required_state_diff(tx_hash: str, rpc_url: str) -> dict[str, Any]:
    """The real chain's own account/storage diff for this transaction."""
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
    # diffMode's post only lists changed fields per account, and identifies
    # destroyed accounts implicitly - reshape into an explicit diff.
    return _normalize_required_state_diff(result["pre"], result["post"])


def _normalize_required_state_diff(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    modified_accounts = {}
    for addr, post_acc in post.items():
        pre_acc = pre.get(addr, {})
        acc: dict[str, Any] = {
            "nonce": _hex(post_acc.get("nonce", pre_acc.get("nonce", 0))),
            "balance": post_acc.get("balance", pre_acc.get("balance", "0x0")),
        }
        if "code" in post_acc:
            acc["code"] = post_acc["code"]
        acc["modifiedStorage"] = dict(post_acc.get("storage", {}))
        modified_accounts[addr] = acc

    # An account in `pre` but missing from `post` was destroyed.
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
    result = subprocess.run(
        ["cast", "keccak", "0x" + data.hex()],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _compute_logs_hash(logs: list[dict[str, Any]]) -> str:
    """`keccak256(rlp(logs))`, from a receipt's raw `logs` entries."""
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


def _build_traced_replay_fixture(
    test_name: str,
    tx: dict[str, Any],
    block: dict[str, Any],
    prestate: dict[str, dict[str, Any]],
    logs_hash: str,
    fork: str,
) -> dict[str, Any]:
    """Assemble a state test from a real transaction and its traced pre-state."""
    # fill in fields the tracer omitted; evmone rejects explicit zero-value storage
    pre = {addr: _normalize_prestate_account(acc) for addr, acc in prestate.items()}
    return {
        test_name: {
            "env": _env_from_block(block),
            "pre": pre,
            "transaction": _tx_from_mainnet(tx),
            "post": _placeholder_post(fork, logs_hash),
            "_info": {"hash": tx["hash"], "chainId": tx.get("chainId", "0x1")},
        }
    }


_MISMATCH_RE = re.compile(
    r"Expected equality of these values:\n"
    r"\s*(?P<actual_expr>\S+)\n"
    r"\s*Which is: (?P<actual>0x[0-9a-fA-F]+)\n"
    r"\s*(?P<expected_expr>\S+)\n"
    r"\s*Which is: (?P<expected>0x[0-9a-fA-F]+)"
)


def _solve_expected_state_root(fixture_path: Path, evmone_statetest_bin: Path) -> str:
    """Run evmone-statetest against a placeholder state root and recover
    the real value from its mismatch output."""
    result = subprocess.run(
        [str(evmone_statetest_bin), str(fixture_path), "--gtest_filter=*", "--gtest_print_time=0"],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    state_root = None
    for m in _MISMATCH_RE.finditer(output):
        if m.group("actual_expr") == "state_root":
            state_root = m.group("actual")

    if state_root is None:
        raise RuntimeError(f"could not recover state root from evmone-statetest output:\n{output}")
    return state_root


def _patch_expected_state_root(fixture_path: Path, state_root: str) -> None:
    """Write the recovered state root (see `_solve_expected_state_root`) back
    into the fixture."""
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    (cases,) = data[test_name]["post"].values()
    cases[0]["hash"] = state_root
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")


def _patch_required_status_and_state_diff(
    fixture_path: Path,
    required_status: str | None,
    required_state_diff: dict[str, Any] | None,
) -> None:
    """Write `requiredStatus`/`requiredStateDiff` into a fixture's
    existing case, in place - our own extension, not part of EEST."""
    if required_status is None and required_state_diff is None:
        return
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    (cases,) = data[test_name]["post"].values()
    case = cases[0]
    if required_status is not None:
        case["requiredStatus"] = required_status
    if required_state_diff is not None:
        case["requiredStateDiff"] = required_state_diff
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")


def build_fixture_for_tx(
    tx_hash: str,
    rpc_url: str,
    evmone_statetest_bin: Path,
    output_path: Path,
    test_name: str | None = None,
) -> Path:
    """Build an EEST state-test fixture for `tx_hash` and write it to
    `output_path`. `test_name` defaults to `tx-<first 10 hex chars>`."""
    tx = _rpc(rpc_url, "eth_getTransactionByHash", [tx_hash])
    if tx is None:
        raise ValueError(f"transaction not found: {tx_hash}")
    block = _rpc(rpc_url, "eth_getBlockByNumber", [tx["blockNumber"], False])
    prestate = _fetch_prestate(tx_hash, rpc_url)
    required_diff = _fetch_required_state_diff(tx_hash, rpc_url)
    receipt = _fetch_receipt(tx_hash, rpc_url)
    required_status = receipt["status"]
    logs_hash = _compute_logs_hash(receipt["logs"])

    name = test_name or f"tx-{tx_hash[2:12]}"
    fork = _fork_for_block(int(tx["blockNumber"], 16))
    fixture = _build_traced_replay_fixture(name, tx, block, prestate, logs_hash, fork)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(fixture, indent=2) + "\n")

    state_root = _solve_expected_state_root(output_path, evmone_statetest_bin)
    _patch_expected_state_root(output_path, state_root)
    _patch_required_status_and_state_diff(output_path, required_status, required_diff)

    try:
        fixture_replay(output_path, evmone_statetest_bin)
    except ReplayMismatch as e:
        print(f"WARNING: {e}", file=sys.stderr)

    return output_path
