"""Download solc binaries from GitHub releases or CircleCI b_ubu_static artifacts."""

import os
import shutil
import tempfile
from pathlib import Path

import requests

_GITHUB_REPO = "argotorg/solidity"
_CIRCLECI_PROJECT_SLUG = "gh/argotorg/solidity"
_GITHUB_API = "https://api.github.com"
_CIRCLECI_API = "https://circleci.com/api/v2"
_ARTIFACT_NAME = "solc-static-linux"
_JOB_NAME = "b_ubu_static"
_HTTP_TIMEOUT = 60


class FetchError(Exception):
    pass


class _NotFound(Exception):
    """Internal: ref doesn't match this source. Caller should try the next one."""


def fetch_solc(ref: str, output: Path, force: bool) -> str:
    """Download solc for `ref` (release tag or branch) to `output`. Returns a one-line description of the source."""
    if output.exists() and not force:
        raise FetchError(f"refusing to overwrite existing file: {output} (pass --force to override)")

    try:
        return _fetch_release(ref, output)
    except _NotFound as e:
        release_err = str(e)

    try:
        return _fetch_branch(ref, output)
    except _NotFound as e:
        raise FetchError(
            f"ref '{ref}' is neither a release tag nor a branch with a successful "
            f"{_JOB_NAME} build.\n"
            f"  release lookup: {release_err}\n"
            f"  branch lookup:  {e}"
        )


def _fetch_release(ref: str, output: Path) -> str:
    url = f"{_GITHUB_API}/repos/{_GITHUB_REPO}/releases/tags/{ref}"
    response = requests.get(url, headers=_github_headers(), timeout=_HTTP_TIMEOUT)
    if response.status_code == 404:
        raise _NotFound(f"no release tagged '{ref}' on {_GITHUB_REPO}")
    response.raise_for_status()

    release = response.json()
    for asset in release.get("assets", []):
        if asset.get("name") == _ARTIFACT_NAME:
            _download(asset["browser_download_url"], output, headers=_github_headers())
            return f"GitHub release {release.get('tag_name', ref)} ({asset['browser_download_url']})"

    raise FetchError(
        f"release '{ref}' has no '{_ARTIFACT_NAME}' asset "
        f"(found: {[a.get('name') for a in release.get('assets', [])]})"
    )


def _fetch_branch(ref: str, output: Path) -> str:
    headers = _circleci_headers()

    pipelines_url = f"{_CIRCLECI_API}/project/{_CIRCLECI_PROJECT_SLUG}/pipeline"
    pipelines = _circleci_get(pipelines_url, params={"branch": ref}, headers=headers).get("items", [])
    if not pipelines:
        raise _NotFound(f"no pipelines found for branch '{ref}' on {_CIRCLECI_PROJECT_SLUG}")

    # Skip nightly/scheduled pipelines, which often run sanitizer-only workflows
    # without the static build job. We want commit-triggered (webhook/api) pipelines.
    candidates = [p for p in pipelines if p.get("trigger", {}).get("type") != "schedule"]
    if not candidates:
        raise FetchError(
            f"branch '{ref}' has only scheduled pipelines, no commit-triggered runs"
        )
    pipeline = max(candidates, key=lambda p: p["created_at"])
    pipeline_num = pipeline.get("number", "?")
    commit_hash = pipeline.get("vcs", {}).get("revision", "?")

    workflows = _circleci_get(
        f"{_CIRCLECI_API}/pipeline/{pipeline['id']}/workflow", headers=headers
    ).get("items", [])
    if not workflows:
        raise FetchError(f"pipeline #{pipeline_num} for branch '{ref}' has no workflows")

    job = None
    for wf in sorted(workflows, key=lambda w: w["created_at"], reverse=True):
        wf_jobs = _circleci_get(
            f"{_CIRCLECI_API}/workflow/{wf['id']}/job", headers=headers
        ).get("items", [])
        match = next((j for j in wf_jobs if j.get("name") == _JOB_NAME), None)
        if match is not None:
            job = match
            break
    if job is None:
        raise FetchError(
            f"job '{_JOB_NAME}' not found in any workflow of pipeline #{pipeline_num} "
            f"for branch '{ref}'"
        )
    if job.get("status") != "success":
        raise FetchError(
            f"job '{_JOB_NAME}' on latest pipeline #{pipeline_num} for branch '{ref}' "
            f"has status '{job.get('status')}', not 'success'. "
            "Wait for the build to finish or pick a different ref."
        )

    job_number = job["job_number"]
    artifacts = _circleci_get(
        f"{_CIRCLECI_API}/project/{_CIRCLECI_PROJECT_SLUG}/{job_number}/artifacts",
        headers=headers,
    ).get("items", [])
    artifact = next((a for a in artifacts if a.get("path") == _ARTIFACT_NAME), None)
    if artifact is None:
        raise FetchError(
            f"job '{_JOB_NAME}' #{job_number} has no '{_ARTIFACT_NAME}' artifact "
            f"(found: {[a.get('path') for a in artifacts]})"
        )

    _download(artifact["url"], output, headers=headers)
    return (
        f"CircleCI {_JOB_NAME} job #{job_number} on branch '{ref}' "
        f"(pipeline #{pipeline_num}, commit {commit_hash[:8]})"
    )


def _circleci_get(url: str, headers: dict, params: dict | None = None) -> dict:
    response = requests.get(url, params=params or {}, headers=headers, timeout=_HTTP_TIMEOUT)
    if not response.ok:
        raise FetchError(f"CircleCI API request failed ({response.status_code}): GET {url}")
    return response.json()


def _download(url: str, dest: Path, headers: dict) -> None:
    """Stream `url` to `dest` atomically: write to a sibling tempfile, then rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # delete=False so the file survives close() for os.replace to move it.
    # The finally below cleans up on failure; missing_ok handles the success path
    # where the file has already been renamed away.
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with requests.get(url, headers=headers, stream=True, timeout=_HTTP_TIMEOUT) as response:
                response.raise_for_status()
                shutil.copyfileobj(response.raw, tmp)
            tmp.close()
            os.chmod(tmp_path, 0o755)
            os.replace(tmp_path, dest)
        finally:
            tmp_path.unlink(missing_ok=True)


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_READ_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _circleci_headers() -> dict:
    token = os.environ.get("CIRCLECI_TOKEN")
    return {"Circle-Token": token} if token else {}
