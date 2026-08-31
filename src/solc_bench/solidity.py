import copy
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager

from solc_bench.config import PIPELINE_CONFIGS


def resolve_solc_settings(pipeline, no_optimize, ethdebug=False):
    """Build solc_settings for a pipeline, applying --no-optimize if set."""
    solc_settings = copy.deepcopy(PIPELINE_CONFIGS[pipeline]["solc_settings"])
    if ethdebug:
        if pipeline != "ir":
            raise ValueError("ETHDebug output is only supported with the IR pipeline")
        if not no_optimize:
            raise ValueError(
                "ETHDebug output does not support the optimizer yet; "
                "pass --no-optimize"
            )
        solc_settings["experimental"] = True
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


_PREFLIGHT_INPUT = {
    "language": "Solidity",
    "sources": {"preflight.sol": {"content": "contract C {}"}},
    "settings": {"outputSelection": {"*": {"*": ["evm.bytecode.object"]}}},
}


def check_solc_flags(solc, extra_flags):
    """Compile a trivial input to verify extra solc flags are actually usable.

    Raises ValueError carrying solc's own diagnostics. Without this a typo only
    surfaces once the suite has already been running for a while, and a flag
    that makes solc emit non-JSON would instead show up as every benchmark
    silently producing no metrics.
    """
    if not extra_flags:
        return

    quoted = " ".join(extra_flags)
    result = subprocess.run(
        [solc, *extra_flags, "--standard-json"],
        input=json.dumps(_PREFLIGHT_INPUT),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(
            f"solc rejected --extra-solc-flags {quoted!r} "
            f"(exit {result.returncode}): {detail[0] if detail else 'no diagnostics'}"
        )

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError(
            f"--extra-solc-flags {quoted!r} made solc emit output that is not "
            "standard-json; benchmarks would record no metrics"
        )

    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    if errors:
        message = errors[0].get("formattedMessage") or errors[0].get("message", "")
        raise ValueError(
            f"--extra-solc-flags {quoted!r} caused a compilation error: "
            f"{message.strip().splitlines()[0] if message else 'unknown error'}"
        )


def _serialized_json_size(value):
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _ethdebug_output_size(output):
    total_size = 0

    ethdebug = output.get("ethdebug")
    if isinstance(ethdebug, dict):
        for artifact in ("resources", "compilation"):
            if artifact in ethdebug:
                total_size += _serialized_json_size(ethdebug[artifact])

    contracts = output.get("contracts", {})
    if not isinstance(contracts, dict):
        return total_size

    for source_contracts in contracts.values():
        if not isinstance(source_contracts, dict):
            continue
        for contract_data in source_contracts.values():
            if not isinstance(contract_data, dict):
                continue
            evm = contract_data.get("evm", {})
            if not isinstance(evm, dict):
                continue
            for bytecode_type in ("bytecode", "deployedBytecode"):
                bytecode = evm.get(bytecode_type, {})
                if not isinstance(bytecode, dict):
                    continue
                ethdebug_program = bytecode.get("ethdebug")
                if ethdebug_program is not None:
                    total_size += _serialized_json_size(ethdebug_program)

    return total_size


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

    creation_size = 0
    runtime_size = 0
    contracts = output.get("contracts", {})
    for source_contracts in contracts.values():
        for contract_data in source_contracts.values():
            evm = contract_data.get("evm", {})
            creation = evm.get("bytecode", {}).get("object", "")
            runtime = evm.get("deployedBytecode", {}).get("object", "")
            if creation:
                creation_size += len(creation) // 2
            if runtime:
                runtime_size += len(runtime) // 2

    if creation_size > 0:
        metrics["creation_size"] = creation_size
    if runtime_size > 0:
        metrics["runtime_size"] = runtime_size

    ethdebug_size = _ethdebug_output_size(output)
    if ethdebug_size > 0:
        metrics["ethdebug_size"] = ethdebug_size

    return metrics


@contextmanager
def wrap_sol_as_standard_json(sol_path, solc_settings, ethdebug=False):
    """Wrap a .sol file into a temporary standard-json input file.

    solc_settings should include optimizer, pipeline flags, etc.
    """
    with open(sol_path, encoding="utf-8") as f:
        source = f.read()

    settings = {"outputSelection": {"*": {"*": ["*"]}}}
    settings.update(solc_settings)
    if ethdebug:
        enable_ethdebug_outputs(settings)

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
def override_json_settings(json_path, solc_settings, ethdebug=False):
    """Copy a standard-json input with overridden pipeline settings.

    Preserves sources, language, and existing settings like outputSelection.
    Only overrides the keys present in solc_settings.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    settings = data.get("settings", {})
    settings.update(solc_settings)
    if ethdebug:
        enable_ethdebug_outputs(settings)
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


def enable_ethdebug_outputs(settings):
    """Add ETHDebug program and resources outputs to standard-json settings."""
    settings.setdefault("debug", {})["debugInfo"] = [
        "location",
        "snippet",
        "ast-id",
        "ethdebug",
    ]

    output_selection = settings.setdefault("outputSelection", {})
    all_files = output_selection.setdefault("*", {})
    all_contracts = all_files.setdefault("*", [])
    if not isinstance(all_contracts, list):
        raise ValueError("settings.outputSelection.*.* must be an array")

    for artifact in (
        "evm.bytecode.ethdebug",
        "evm.deployedBytecode.ethdebug",
        "ethdebug.resources",
        "ethdebug.compilation",
    ):
        if artifact not in all_contracts:
            all_contracts.append(artifact)


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
