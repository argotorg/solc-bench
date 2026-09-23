"""Tests for solc_bench.add_benchmark.capture_contract."""

import json
from collections import Counter
from unittest.mock import patch

import tomlkit

from solc_bench.add_benchmark.capture_contract import capture_contract
from solc_bench.add_benchmark.fixture_builder import CURRENT_FORK_ACTIVATION_BLOCK

ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
SENDER = "0x1111111111111111111111111111111111111111"
MINER = "0x3333333333333333333333333333333333333333"
TX_HASH_1 = "0x" + "aa" * 32
TX_HASH_2 = "0x" + "bb" * 32

#   CALLDATASIZE ISZERO PUSH1 0x0a JUMPI PUSH1 0x2a PUSH1 0x00 SSTORE JUMPDEST STOP
CODE = "0x3615600a57602a6000555b00"
SLOT = "0x0000000000000000000000000000000000000000000000000000000000000000"
STORED_VALUE = "0x000000000000000000000000000000000000000000000000000000000000002a"
SENT_VALUE = "0xde0b6b3a7640000"  # 1 ETH

PRESTATE = {
    SENDER: {"balance": "0x8ac7230489e80000", "nonce": 1, "code": "0x", "storage": {}},
    ADDRESS: {"balance": "0x0", "code": CODE, "storage": {}},
}
BLOCK = {
    "miner": MINER,
    "gasLimit": "0x1c9c380",
    "number": hex(CURRENT_FORK_ACTIVATION_BLOCK),
    "timestamp": "0x669",
    "mixHash": "0x" + "ab" * 32,
    "baseFeePerGas": "0x0",
}
RECEIPT = {"status": "0x1", "logs": []}


def _base_tx(tx_hash, **overrides):
    tx = {
        "hash": tx_hash,
        "nonce": "0x1",
        "gas": "0x186a0",
        "to": ADDRESS,
        "value": "0x0",
        "input": "0x",
        "from": SENDER,
        "type": "0x0",
        "gasPrice": "0x3b9aca00",
        "blockNumber": BLOCK["number"],
    }
    tx.update(overrides)
    return tx


# tx1: a plain value transfer.
# tx2: a call with calldata and no value. Only ADDRESS's storage changes.
TX_DATA = {
    TX_HASH_1: (
        _base_tx(TX_HASH_1, value=SENT_VALUE),
        {
            SENDER: {"balance": "0x7ce65933047cb200", "nonce": 2},  # pre - SENT_VALUE - 21019 gas * gasPrice
            ADDRESS: {"balance": SENT_VALUE},
        },
    ),
    TX_HASH_2: (
        _base_tx(TX_HASH_2, input="0x12345678"),
        {
            SENDER: {"balance": "0x8ac6fbbcd0e72e00", "nonce": 2},  # pre - 43189 gas * gasPrice
            ADDRESS: {"storage": {SLOT: STORED_VALUE}},
        },
    ),
}


class FakeResponse:
    def __init__(self, result):
        self._result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": self._result}


def _fake_rpc_post(url, json, timeout):
    method = json["method"]
    if method == "eth_getTransactionByHash":
        (tx_hash,) = json["params"]
        result, _ = TX_DATA[tx_hash]
    elif method == "eth_getBlockByNumber":
        result = BLOCK
    elif method == "eth_getTransactionReceipt":
        result = RECEIPT
    elif method == "debug_traceTransaction":
        tx_hash = json["params"][0]
        assert json["params"][1].get("tracerConfig", {}).get("diffMode") is True
        _, diff_post = TX_DATA[tx_hash]
        result = {"pre": PRESTATE, "post": diff_post}
    else:
        raise AssertionError(f"unexpected RPC method: {method}")
    return FakeResponse(result)


def test_capture_contract_smoke(evmone, tmp_path):
    counts = Counter({"0xaaaaaaaa": 5, "0xbbbbbbbb": 3})
    examples = {"0xaaaaaaaa": TX_HASH_1, "0xbbbbbbbb": TX_HASH_2}
    names = {"0xaaaaaaaa": "foo", "0xbbbbbbbb": "bar"}
    with (
        patch("solc_bench.add_benchmark.capture_contract.discover_popular_calls",
              return_value=(counts, examples, names)),
        patch("solc_bench.add_benchmark.fixture_builder.requests.post", side_effect=_fake_rpc_post),
    ):
        written = capture_contract(
            ADDRESS, "http://rpc", "key", evmone, tmp_path,
            end_block=100, limit=500, target_address=ADDRESS
        )

    assert {p.name for p in written} == {"foo-aaaaaaaa.json", "bar-bbbbbbbb.json"}
    for path in written:
        assert path.is_file()
        data = json.loads(path.read_text())
        (test_name,) = data.keys()
        (cases,) = data[test_name]["post"].values()
        case = cases[0]
        assert case["hash"] != "0x0"  # solved by a real evmone run
        modified = case["requiredStateDiff"]["modifiedAccounts"][ADDRESS]
        if test_name == "foo":  # tx1: value transfer only
            assert modified["balance"] == SENT_VALUE
            assert modified["modifiedStorage"] == {}
        else:  # bar: tx2, storage write only
            assert modified["balance"] == "0x0"
            assert modified["modifiedStorage"] == {SLOT: STORED_VALUE}

    doc = tomlkit.load((tmp_path / "targets.toml").open())
    (entry,) = doc["target"]
    assert entry["address"] == ADDRESS
    assert entry["discovery_address"] == ADDRESS
    assert entry["discovery_end_block"] == 100
    assert entry["discovery_limit"] == 500
