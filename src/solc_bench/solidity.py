import copy
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager

from solc_bench.config import PIPELINE_CONFIGS


def resolve_solc_settings(pipeline, no_optimize):
    """Build solc_settings for a pipeline, applying --no-optimize if set."""
    solc_settings = copy.deepcopy(PIPELINE_CONFIGS[pipeline]["solc_settings"])
    if no_optimize:
        solc_settings["optimizer"] = {"enabled": False}
    solc_settings.setdefault("metadata", {}).update({
        "bytecodeHash": "none",
        "appendCBOR": False,
    })
    return solc_settings


def get_solc_version(solc):
    """Get the version string from a solc binary.

    Raises FileNotFoundError, PermissionError, or ValueError.
    """
    result = subprocess.run(
        [solc, "--version"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ValueError(f"solc failed (exit {result.returncode}): {stderr or solc}")

    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split("Version: ", 1)[1].strip()

    raise ValueError(f"not a solc binary: {solc}")


def parse_solc_output(stdout):
    """Parse solc standard-json output for bytecode size and error count."""
    metrics = {}

    try:
        output = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics

    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    metrics["errors"] = len(errors)
    if errors:
        metrics["error_messages"] = [
            e.get("formattedMessage", e.get("message", "")) for e in errors
        ]

    total_size = 0
    contracts = output.get("contracts", {})
    for source_contracts in contracts.values():
        for contract_data in source_contracts.values():
            evm = contract_data.get("evm", {})
            bytecode = evm.get("bytecode", {})
            obj = bytecode.get("object", "")
            if obj:
                total_size += len(obj) // 2

    if total_size > 0:
        metrics["bytecode_size"] = total_size

    return metrics


@contextmanager
def wrap_sol_as_standard_json(sol_path, solc_settings):
    """Wrap a .sol file into a temporary standard-json input file.

    solc_settings should include optimizer, pipeline flags, etc.
    """
    with open(sol_path, encoding="utf-8") as f:
        source = f.read()

    settings = {"outputSelection": {"*": {"*": ["*"]}}}
    settings.update(solc_settings)

    standard_input = {
        "language": "Solidity",
        "sources": {
            os.path.basename(sol_path): {
                "content": source,
            }
        },
        "settings": settings,
    }

    with write_temp_json(standard_input) as path:
        yield path


@contextmanager
def override_json_settings(json_path, solc_settings):
    """Copy a standard-json input with overridden pipeline settings.

    Preserves sources, language, and existing settings like outputSelection.
    Only overrides the keys present in solc_settings.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    settings = data.get("settings", {})
    settings.update(solc_settings)
    data["settings"] = settings

    with write_temp_json(data) as path:
        yield path


@contextmanager
def write_temp_json(data):
    """Write data to a temporary JSON file, yield its path, remove on exit."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="solc-bench-", delete=False, encoding="utf-8"
    )
    try:
        json.dump(data, tmp)
        tmp.close()
        yield tmp.name
    finally:
        os.remove(tmp.name)


def validate_standard_json(path):
    """Check that a JSON file looks like a solc standard-json input.

    Raises ValueError if it's not valid.
    """
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON in {path}: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")

    # Same root keys solc accepts in StandardCompiler.cpp:checkRootKeys
    valid_keys = {"auxiliaryInput", "language", "settings", "sources"}
    unknown = set(data.keys()) - valid_keys
    if unknown:
        raise ValueError(f"{path}: unknown root keys: {', '.join(sorted(unknown))}")

    if "language" not in data or "sources" not in data:
        raise ValueError(
            f"{path} is not a valid standard-json input"
            " (missing 'language' or 'sources')"
        )
