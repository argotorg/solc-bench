"""Extract standard-json inputs from Forge projects.

Runs `forge build --build-info` to generate build-info JSON, then
extracts the standard-json input from it. This is used to add new
benchmark projects to the repository.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PIPELINES = ["legacy", "ir"]


def extract_inputs(solc, project_dir, output_dir):
    """Generate standard-json input files from a Forge project.

    Creates {project}-{pipeline}.json files in output_dir for each pipeline.
    """
    project_dir = os.path.abspath(project_dir.rstrip("/"))
    project_name = os.path.basename(project_dir)

    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"{project_dir} is not a directory")

    if not shutil.which("forge"):
        raise FileNotFoundError("forge not found in PATH")

    os.makedirs(output_dir, exist_ok=True)

    for pipeline in PIPELINES:
        output_file = os.path.join(output_dir, f"{project_name}-{pipeline}.json")

        if os.path.exists(output_file):
            print(
                f"  {project_name} ({pipeline}): already exists, skipping",
                file=sys.stderr,
            )
            continue

        print(f"  {project_name} ({pipeline})...", file=sys.stderr, end="", flush=True)

        clean_forge_output(project_dir)

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
        if pipeline == "ir":
            forge_cmd.append("--via-ir")

        log_file = os.path.join(output_dir, f"{project_name}-{pipeline}.forge.log")
        with open(log_file, "w") as log:
            subprocess.run(
                forge_cmd,
                cwd=project_dir,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )

        build_info_dir = os.path.join(project_dir, "out", "build-info")
        build_info_files = (
            sorted(Path(build_info_dir).glob("*.json"))
            if os.path.isdir(build_info_dir)
            else []
        )

        if build_info_files:
            # Forge adds extra keys (e.g. allowPaths) that solc rejects.
            # See https://github.com/foundry-rs/compilers/pull/35
            with open(build_info_files[0]) as f:
                build_info = json.load(f)
            std_input = build_info.get("input", {})
            filtered = {
                k: std_input[k]
                for k in ("language", "sources", "settings")
                if k in std_input
            }
            with open(output_file, "w") as f:
                json.dump(filtered, f)
            os.unlink(log_file)
            print(" OK", file=sys.stderr)
        else:
            print(f" FAILED (no build-info, see {log_file})", file=sys.stderr)

        clean_forge_output(project_dir)


def clean_forge_output(project_dir):
    """Remove forge build artifacts."""
    for d in ["out", "cache"]:
        p = os.path.join(project_dir, d)
        if os.path.exists(p):
            shutil.rmtree(p)
