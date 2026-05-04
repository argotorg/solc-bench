"""Gas benchmarking via forge test --gas-report --json."""

import json
import subprocess
import sys
from pathlib import Path


def aggregate_gas(report):
    """Sum deployment_gas + method_gas across all contracts."""
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
    return {"deployment_gas": deployment, "method_gas": method}


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


def ensure_project(benchmark_dir, name, source, version):
    """Ensure <benchmark-dir>/<name>/ exists as a Forge project. Clone if missing."""
    project_dir = Path(benchmark_dir) / name
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
