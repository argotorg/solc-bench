"""Extract standard-json inputs from Forge projects.

Runs `forge build --build-info` to generate build-info JSON, then
extracts the standard-json input from it. This is used to add new
benchmark projects to the repository.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def extract_inputs(solc, project_dir, output_dir):
    """Generate a standard-json input file from a Forge project.

    Creates {project}.json in output_dir with the sources and base settings.
    Pipeline and optimizer settings are applied at runtime by the run command.
    """
    project_dir = Path(project_dir).resolve()
    project_name = project_dir.name
    output_dir = Path(output_dir)
    if "/" in solc:
        solc = str(Path(solc).resolve())

    if not project_dir.is_dir():
        raise FileNotFoundError(f"{project_dir} is not a directory")

    if not (project_dir / "foundry.toml").is_file():
        raise FileNotFoundError(
            f"{project_dir} is not a Forge project (no foundry.toml)"
        )

    if not shutil.which("forge"):
        raise FileNotFoundError("forge not found in PATH")

    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"{project_name}.json"

    if output_file.exists():
        print(f"  {project_name}: already exists, skipping", file=sys.stderr)
        return True

    print(f"  {project_name}...", file=sys.stderr, end="", flush=True)

    clean_forge_output(project_dir)
    try:
        forge_cmd = [
            "forge",
            "build",
            "--use",
            solc,
            "--optimize",
            "--offline",
            "--no-cache",
            "--build-info",
        ]

        log_file = output_dir / f"{project_name}.forge.log"
        with open(log_file, "w", encoding="utf-8") as log:
            result = subprocess.run(
                forge_cmd,
                cwd=project_dir,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )

        if result.returncode != 0:
            print(
                f" FAILED (forge exit {result.returncode}, see {log_file})",
                file=sys.stderr,
            )
            return False

        build_info_dir = project_dir / "out" / "build-info"
        if build_info_dir.is_dir():
            build_info_files = sorted(build_info_dir.glob("*.json"))
        else:
            build_info_files = []

        if not build_info_files:
            print(f" FAILED (no build-info, see {log_file})", file=sys.stderr)
            return False

        # Forge adds extra keys (e.g. allowPaths) that solc rejects.
        # See https://github.com/foundry-rs/compilers/pull/35
        with open(build_info_files[0], encoding="utf-8") as f:
            build_info = json.load(f)
        std_input = build_info.get("input", {})
        filtered = {
            k: std_input[k]
            for k in ("language", "sources", "settings")
            if k in std_input
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(filtered, f)
        log_file.unlink()
        print(" OK", file=sys.stderr)
        return True
    finally:
        clean_forge_output(project_dir)


def clean_forge_output(project_dir):
    """Remove forge build artifacts."""
    for d in ["out", "cache"]:
        p = project_dir / d
        if p.exists():
            shutil.rmtree(p)
