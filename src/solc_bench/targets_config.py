"""Read targets.toml: which standard-json source to compile a contract
from, which fixture address its bytecode should be swapped into, how to
re-run the discovery query that found it, and its library/immutable
values.
"""

from pathlib import Path

import tomlkit


def read_targets_config(path: Path) -> list[dict]:
    """Each `[[target]]` entry: `address`, `discovery_address`,
    `discovery_end_block`/`discovery_limit` (how to replay the same
    discovery query later), `standard_json`, `contract_name`,
    `source_name`, `libraries`/`immutables` (`{name: value}`, from
    `[target.libraries]`/`[target.immutables]`)."""
    doc = tomlkit.load(path.open())
    targets = []
    for entry in doc["target"]:
        targets.append(
            {
                "address": str(entry["address"]).lower(),
                "discovery_address": str(entry["discovery_address"]).lower(),
                "discovery_end_block": entry["discovery_end_block"],
                "discovery_limit": entry["discovery_limit"],
                "standard_json": entry["standard_json"],
                "contract_name": entry["contract_name"],
                "source_name": entry.get("source_name"),
                "libraries": dict(entry.get("libraries", {})),
                "immutables": dict(entry.get("immutables", {})),
            }
        )
    return targets
