"""Compile a contract's source and swap its bytecode into a fixture's
pre-state, for gas comparison against a candidate solc build.
"""

import json
import re
from pathlib import Path


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


# "11b" reads as "lib", padded with zeros
_DUMMY_LIBRARY_ADDRESS = "0x" + "11b".rjust(40, "0")
_DUMMY_LIBRARY_CODE = "0xfe"  # INVALID opcode - fails loudly if ever actually called
_LIBRARY_DECLARATION_RE = re.compile(r"\blibrary\s+(\w+)")


def _discover_libraries(sources: dict) -> dict[str, str]:
    """Every `library X { ... }` declared anywhere in `sources`, as
    `{name: declaring file}` - a plain source-text scan, no compile."""
    found: dict[str, str] = {}
    for file, entry in sources.items():
        for name in _LIBRARY_DECLARATION_RE.findall(entry.get("content", "")):
            found[name] = file
    return found


def merge_gas_bench_output_selection(output_selection: dict | None) -> dict:
    """`output_selection` with the fields `extract_deployed_bytecode` needs
    added if missing, without dropping what's already requested."""
    output_selection = json.loads(json.dumps(output_selection)) if output_selection else {}
    star = output_selection.setdefault("*", {})
    per_contract = star.setdefault("*", [])
    for field in ("evm.deployedBytecode.object", "evm.deployedBytecode.immutableReferences"):
        if field not in per_contract:
            per_contract.append(field)
    file_level = star.setdefault("", [])
    if "ast" not in file_level:
        file_level.append("ast")
    return output_selection


def merge_target_libraries(sources: dict, targets: list[dict]) -> dict[str, dict[str, str]]:
    """`settings.libraries` linking every library declared in `sources` to
    the address `targets` supply for it, or a dummy address otherwise."""
    libraries: dict[str, str] = {}
    for target in targets:
        libraries.update(target.get("libraries") or {})
    settings_libraries: dict[str, dict[str, str]] = {}
    for name, file in _discover_libraries(sources).items():
        address = libraries.get(name, _DUMMY_LIBRARY_ADDRESS)
        settings_libraries.setdefault(file, {})[name] = address
    return settings_libraries


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


def _patch_immutables(code: str, references: dict, standard_json_output: dict, immutables: dict[str, str]) -> str:
    """Patch declared immutable values directly into `code` at the byte
    offsets solc reported; anything not declared stays zero-filled."""
    if not references:
        return code
    names = _immutable_names_by_id(standard_json_output)
    for ast_id, occurrences in references.items():
        value = immutables.get(names.get(ast_id, ""))
        if value is None:
            continue
        value_hex = value[2:] if value.startswith("0x") else value
        for occurrence in occurrences:
            start, length = occurrence["start"] * 2, occurrence["length"] * 2
            code = code[:start] + value_hex[-length:].rjust(length, "0") + code[start + length :]
    return code


def extract_deployed_bytecode(
    standard_json_output: dict,
    contract_name: str,
    source_name: str | None = None,
    immutables: dict[str, str] | None = None,
) -> str:
    """`contract_name`'s deployed (runtime) bytecode as `"0x..."`, from an
    already-compiled standard_json_output, with every declared immutable
    patched to its value in `immutables`."""
    contract = _select_contract(standard_json_output, contract_name, source_name)
    code = contract["evm"]["deployedBytecode"]["object"]
    references = contract["evm"]["deployedBytecode"].get("immutableReferences", {})
    code = _patch_immutables(code, references, standard_json_output, immutables or {})
    return "0x" + code


def ensure_dummy_library_account(fixture: dict) -> dict:
    """Give the dummy library address (see `merge_target_libraries`) an
    `INVALID`-opcode account, so a call into it fails loudly."""
    (test_name,) = fixture.keys()
    pre = fixture[test_name]["pre"]
    pre.setdefault(
        _DUMMY_LIBRARY_ADDRESS.lower(),
        {"nonce": "0x0", "balance": "0x0", "code": _DUMMY_LIBRARY_CODE, "storage": {}},
    )
    return fixture


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
_GAS_HEADROOM_FACTOR = 1.1


def maximize_gas_headroom(fixture: dict) -> dict:
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
