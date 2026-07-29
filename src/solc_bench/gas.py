"""Gas benchmarking via forge test --gas-report --json."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def gas_unsupported_reason(config):
    """Why this benchmark can't be gas-measured, or None if it can.

    ensure_project clones at a git tag, so entries whose `version` is a commit
    SHA or a path to a source file are out of reach.
    """
    source = config.get("source")
    version = config.get("version")
    if not (source and version):
        return "no source/version in benchmarks.toml"
    if _SHA_RE.match(version):
        return f"version {version[:12]} is a commit SHA, not a tag"
    if "/" in version or version.endswith(".sol"):
        return f"version {version} is not a git tag"
    return None


def forge_available():
    return shutil.which("forge") is not None


def aggregate_gas(report):
    """Sum deployment_gas + method_gas, keep per-function detail."""
    deployment = sum(
        c["deployment"]["gas"]
        for c in report
        if c.get("deployment")
    )
    method = sum(
        f["mean"] * f["calls"]
        for c in report
        for f in c.get("functions", {}).values()
    )
    functions = {}
    for c in report:
        contract_name = c.get("contract", "").rsplit(":", 1)[-1]
        for sig, data in c.get("functions", {}).items():
            functions[f"{contract_name}.{sig}"] = data
    return {
        "deployment_gas": deployment,
        "method_gas": method,
        "functions": functions,
    }


def run_gas_benchmark(solc, project_dir, via_ir, log_path=None):
    """Run forge gas-report once for one pipeline.

    Tests partially failing is OK: forge emits a gas-report for tests
    that did run. metrics_dict is None only if the JSON is unparseable.
    When had_failures is true and log_path is provided, a diagnostic log is written there.
    """
    cmd = [
        "forge", "test", "--gas-report", "--json",
        "--use", str(solc),
        "--offline",
    ]
    if via_ir:
        cmd.append("--via-ir")
    result = subprocess.run(
        cmd, cwd=project_dir, capture_output=True, text=True,
    )

    had_failures = result.returncode != 0
    if had_failures and log_path is not None:
        diag_cmd = [c for c in cmd if c != "--json"]
        log = (
            f"$ {' '.join(cmd)}\n\nexit code: {result.returncode}\n\n"
            f"NOTE: --json suppresses forge's per-test pass/fail output.\n"
            f"To see which tests failed, re-run without --json:\n"
            f"  cd {project_dir} && {' '.join(diag_cmd)}\n"
        )
        if result.stderr:
            log += f"\n--- stderr ---\n{result.stderr}\n"
        Path(log_path).write_text(log, encoding="utf-8")

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, had_failures

    return aggregate_gas(report), had_failures


def _project_version(project_dir):
    """Return the git tag at HEAD of project_dir, or None if not on a tag."""
    result = subprocess.run(
        ["git", "-C", str(project_dir), "describe", "--tags", "--exact-match"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_project(project_root, name, source, version):
    """Ensure <project-root>/<name>/ exists as a Forge project. Clone if missing."""
    project_root = Path(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    project_dir = project_root / name
    if (project_dir / "foundry.toml").is_file():
        existing = _project_version(project_dir)
        if existing != version:
            raise RuntimeError(
                f"{project_dir} is at {existing or 'unknown version'} but "
                f"the TOML requests {version}. Remove the directory to re-clone."
            )
        return project_dir
    if not (source and version):
        return None
    print(f"  cloning {source}@{version} into {project_dir}...", file=sys.stderr)
    subprocess.run(
        [
            "git", "clone",
            "--depth", "1",
            "--recurse-submodules",
            "--shallow-submodules",
            "-b", version,
            source,
            str(project_dir),
        ],
        check=True,
    )
    return project_dir
