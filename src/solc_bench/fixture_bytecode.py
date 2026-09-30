"""Swap a contract's freshly compiled bytecode into a fixture's pre-state,
for gas comparison against a candidate solc build.
"""

import json
from pathlib import Path

import tomlkit

# What `extract_deployed_bytecode` reads from the standard-json output.
_DEPLOYED_BYTECODE_OUTPUTS = (
    "evm.deployedBytecode.object",
    "evm.deployedBytecode.immutableReferences",
    "evm.deployedBytecode.linkReferences",
)


def read_targets_config(path: Path) -> list[dict]:
    """Each `[[target]]` in a targets.toml: the fixture `address` to swap
    code into, the `contract_name` (and `source_name`, if ambiguous) to
    compile it from, and the `libraries`/`immutables` it was deployed with."""
    with open(path, encoding="utf-8") as f:
        doc = tomlkit.load(f)
    targets = []
    for entry in doc["target"]:
        if "contract_name" not in entry:
            raise ValueError(f"{path}: target {entry['address']} has no contract_name")
        targets.append(
            {
                "address": str(entry["address"]).lower(),
                "contract_name": str(entry["contract_name"]),
                "source_name": entry.get("source_name"),
                "libraries": dict(entry.get("libraries", {})),
                "immutables": dict(entry.get("immutables", {})),
            }
        )
    return targets


def merge_gas_bench_output_selection(output_selection: dict | None) -> dict:
    """`output_selection` with the fields `extract_deployed_bytecode` needs
    added if missing, without dropping what's already requested."""
    output_selection = json.loads(json.dumps(output_selection)) if output_selection else {}
    star = output_selection.setdefault("*", {})
    per_contract = star.setdefault("*", [])
    for field in _DEPLOYED_BYTECODE_OUTPUTS:
        if field not in per_contract:
            per_contract.append(field)
    file_level = star.setdefault("", [])
    if "ast" not in file_level:
        file_level.append("ast")
    return output_selection


def _select_contract(standard_json_output: dict, contract_name: str, source_name: str | None) -> dict:
    contracts = standard_json_output.get("contracts", {})
    if source_name is None:
        if len(contracts) != 1:
            raise ValueError(
                f"{len(contracts)} source file(s); pass source_name explicitly "
                f"(one of {sorted(contracts)})"
            )
        (source_name,) = contracts.keys()
    try:
        return contracts[source_name][contract_name]
    except KeyError as e:
        available = sorted(contracts.get(source_name, {}))
        raise ValueError(f"{contract_name!r} not found in {source_name!r} (available: {available})") from e


def _immutable_names_by_id(standard_json_output: dict) -> dict[str, str]:
    """Every immutable `VariableDeclaration` across all sources' ASTs, as
    `{ast_id: declared name}` - may be declared in a base contract."""
    names: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("nodeType") == "VariableDeclaration" and node.get("mutability") == "immutable":
                names[str(node["id"])] = node["name"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for source in standard_json_output.get("sources", {}).values():
        walk(source.get("ast"))
    return names


def _patch(code: str, occurrence: dict, value: str) -> str:
    """`code` with the bytes at a solc-reported `{start, length}` replaced
    by `value`, left-padded with zeros."""
    start, length = occurrence["start"] * 2, occurrence["length"] * 2
    value_hex = value[2:] if value.startswith("0x") else value
    return code[:start] + value_hex[-length:].rjust(length, "0") + code[start + length :]


def extract_deployed_bytecode(standard_json_output: dict, target: dict) -> str:
    """The target contract's deployed (runtime) bytecode as `"0x..."`, with
    its libraries linked and its immutables patched from `target`
    (immutables it doesn't list stay zero)."""
    contract = _select_contract(standard_json_output, target["contract_name"], target["source_name"])
    deployed = contract["evm"]["deployedBytecode"]
    code = deployed["object"]

    for libraries in deployed.get("linkReferences", {}).values():
        for name, occurrences in libraries.items():
            if name not in target["libraries"]:
                raise ValueError(f"links library {name}, which has no address in [target.libraries]")
            for occurrence in occurrences:
                code = _patch(code, occurrence, target["libraries"][name])

    references = deployed.get("immutableReferences", {})
    names = _immutable_names_by_id(standard_json_output) if references else {}
    for ast_id, occurrences in references.items():
        value = target["immutables"].get(names.get(ast_id, ""))
        if value is None:
            continue
        for occurrence in occurrences:
            code = _patch(code, occurrence, value)
    return "0x" + code


def swap_contract_code(fixture: dict, address: str, new_code: str) -> dict:
    """Return `fixture` (a parsed fixture JSON) with `pre[address].code`
    replaced by `new_code`, in place."""
    address = address.lower()
    (test_name,) = fixture.keys()
    if address not in fixture[test_name]["pre"]:
        raise ValueError(f"{address} not found in pre-state")
    fixture[test_name]["pre"][address]["code"] = new_code
    return fixture


# A build needing more than this over the tx's real gas limit is a
# regression, not normal compiler variance.
_GAS_HEADROOM_FACTOR = 1.05


def add_gas_headroom(fixture: dict) -> dict:
    """Raise the tx's gasLimit and the sender's balance to afford it - only
    safe when the sender balance diff is exempted."""
    (test_name,) = fixture.keys()
    entry = fixture[test_name]
    tx = entry["transaction"]
    block_gas_limit = int(entry["env"]["currentGasLimit"], 16)
    orig_gas_limit = int(tx["gasLimit"][0], 16)
    new_gas_limit = min(int(orig_gas_limit * _GAS_HEADROOM_FACTOR), block_gas_limit)
    price = int(tx["maxFeePerGas"] if "maxFeePerGas" in tx else tx["gasPrice"], 16)

    account = entry["pre"][tx["sender"]]
    extra_cost = (new_gas_limit - orig_gas_limit) * price
    account["balance"] = hex(int(account["balance"], 16) + max(extra_cost, 0))
    tx["gasLimit"] = [hex(new_gas_limit)]
    return fixture
