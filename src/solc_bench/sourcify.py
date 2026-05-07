"""Extract real-world contracts from Sourcify"""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
import tomlkit
from packaging.version import InvalidVersion, Version
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

GROWTHEPIE_URL = "https://api.growthepie.xyz/v1/top_contracts/export_ethereum.json"
SOURCIFY_API = "https://sourcify.dev/server/v2/contract"
MAINNET_CHAIN_ID = 1
# Metadata responses are single-digit KB
# stdJsonInput for big multi-file contracts (USDC, Aave, ...) can be tens of KB and occasionally slow.
METADATA_TIMEOUT = 15
SOURCES_TIMEOUT = 60

_RETRY = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 502, 503, 504],
)

_PRAGMA_RE = re.compile(r"^\s*pragma\s+solidity\s+[^;]+;", re.MULTILINE)
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass
class _AddressEntry:
    address: str
    name: str
    owner_project: str
    usage_category: str
    tx_count: int


@dataclass
class _BenchEntry:
    standard_json: dict
    bench_id: str
    compiler_version: str
    fully_qualified_name: str
    implementation_address: str | None


def _make_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=_RETRY))
    return session


def _get_json(session: requests.Session, url: str, timeout: int, allow_404: bool = False) -> dict | None:
    """GET with retries. Returns None on 404 if allow_404=True, raises otherwise."""
    response = session.get(url, timeout=timeout)
    if allow_404 and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _fetch_top_addresses(session: requests.Session, top_n: int) -> list[_AddressEntry]:
    print(f"Fetching top-{top_n} mainnet contracts from growthepie...", file=sys.stderr)
    items = _get_json(session, GROWTHEPIE_URL, METADATA_TIMEOUT)
    return [
        _AddressEntry(
            address=item["address"].lower(),
            name=item.get("name") or "",
            owner_project=item.get("owner_project") or "",
            usage_category=item.get("usage_category") or "",
            tx_count=int(item.get("txcount_180d") or 0),
        )
        for item in items[:top_n]
    ]


def _sourcify_url(address: str, fields: str) -> str:
    return f"{SOURCIFY_API}/{MAINNET_CHAIN_ID}/{address}?fields={fields}"


def _fetch_metadata(session: requests.Session, address: str) -> dict | None:
    # Sourcify returns 404 for contracts that aren't verified. Treat as a
    # skip, not an error. See https://sourcify.dev/server/api-docs/#/Contract%20Lookup/get-contract
    return _get_json(
        session,
        _sourcify_url(address, "compilation,proxyResolution"),
        METADATA_TIMEOUT,
        allow_404=True,
    )


def _fetch_standard_json(session: requests.Session, address: str) -> dict | None:
    result = _get_json(
        session,
        _sourcify_url(address, "stdJsonInput"),
        SOURCES_TIMEOUT,
        allow_404=True,
    )
    return result.get("stdJsonInput") if result else None


def _resolve_proxy(metadata: dict) -> str | None:
    proxy = metadata.get("proxyResolution") or {}
    if not proxy.get("isProxy"):
        return None
    implementations = proxy.get("implementations") or []
    if not implementations:
        return None
    return implementations[0]["address"].lower()


def _check_version(compilation_metadata: dict, min_version: Version) -> str | None:
    compiler_version = compilation_metadata.get("compilerVersion", "")
    try:
        if Version(compiler_version) < min_version:
            return f"solc {compiler_version}"
    except InvalidVersion:
        return f"unparseable solc version {compiler_version!r}"
    return None


def _ensure_output_selection(standard_json: dict) -> None:
    """Ensure settings.outputSelection is set to the bench default.

    Metadata stripping happens at run time via solidity.resolve_solc_settings.
    """
    standard_json.setdefault("settings", {}).setdefault(
        "outputSelection",
        {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}},
    )


def _relax_pragmas(standard_json: dict, min_version: str) -> None:
    """Rewrite each source's pragma solidity to >={min_version}"""
    relaxed = f"pragma solidity >={min_version};"
    for source in standard_json.get("sources", {}).values():
        content = source.get("content")
        if content:
            source["content"] = _PRAGMA_RE.sub(relaxed, content)


def _safe_bench_id(name: str, address: str) -> str:
    # Last 8 chars, not first: vanity addresses (e.g. Seaport)
    # collide with other contracts with 00000000 on a head slice.
    safe_name = _SAFE_NAME.sub("_", name) if name else "contract"
    return f"{safe_name}-{address[-8:]}"


def _entry_fields(entry: _BenchEntry, address_entry: _AddressEntry, original_address: str) -> dict:
    is_proxy = entry.implementation_address is not None
    # TODO: add ir-ssacfg
    fields = {
        "pipelines": ["evmasm", "ir"],
        "gas": False,
        "tags": ["sourcify", "mainnet"] + (["proxy"] if is_proxy else []),
        "sourcify_version": entry.compiler_version,
        "sourcify_fqn": entry.fully_qualified_name,
        "mainnet_address": entry.implementation_address or original_address,
    }
    if is_proxy:
        fields["proxy_address"] = original_address
    fields["tx_count_180d"] = address_entry.tx_count
    fields["name"] = address_entry.name
    fields["owner_project"] = address_entry.owner_project
    fields["usage_category"] = address_entry.usage_category
    return fields


def _resolve_compilation(
    session: requests.Session, address: str, min_version: Version, label: str = "contract"
) -> tuple[dict, dict] | None:
    """Fetch metadata + version-check. Return (metadata, compilation) or None on skip."""
    metadata = _fetch_metadata(session, address)
    if metadata is None:
        print(f"    skip ({label}): not verified on Sourcify", file=sys.stderr)
        return None
    compilation = metadata.get("compilation") or {}
    skip_reason = _check_version(compilation, min_version)
    if skip_reason:
        print(f"    skip ({label}): {skip_reason}", file=sys.stderr)
        return None
    return metadata, compilation


def _process_contract(
    session: requests.Session, address_entry: _AddressEntry, address: str, min_version: Version
) -> _BenchEntry | None:
    """Fetch + filter one contract. Return _BenchEntry or None."""
    result = _resolve_compilation(session, address, min_version)
    if result is None:
        return None
    metadata, compilation = result

    implementation_address = _resolve_proxy(metadata)
    if implementation_address is not None:
        result = _resolve_compilation(
            session, implementation_address, min_version, label="proxy impl"
        )
        if result is None:
            return None
        _, compilation = result

    bench_address = implementation_address or address

    if compilation.get("language") != "Solidity":
        print(
            f"    skip: language={compilation.get('language')!r}",
            file=sys.stderr,
        )
        return None

    standard_json = _fetch_standard_json(session, bench_address)
    if not standard_json:
        print("    skip: no stdJsonInput in response", file=sys.stderr)
        return None

    _relax_pragmas(standard_json, str(min_version))
    _ensure_output_selection(standard_json)

    return _BenchEntry(
        standard_json=standard_json,
        bench_id=_safe_bench_id(address_entry.name, bench_address),
        compiler_version=compilation.get("compilerVersion", ""),
        fully_qualified_name=compilation.get("fullyQualifiedName", ""),
        implementation_address=implementation_address,
    )


def extract(output_dir: Path | str, top_n: int, min_version: str, force: bool = False) -> int:
    """Extract the top-N most-used mainnet Solidity contracts as a bench suite."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            raise FileExistsError(
                f"output dir is not empty: {output_dir} "
                "(pass --force to overwrite, or choose a different --output-dir)"
            )
        for child in output_dir.iterdir():
            if child.is_file():
                child.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    min_version = Version(min_version)

    with _make_session() as session:
        address_entries = _fetch_top_addresses(session, top_n)
        print(
            f"Looking up {len(address_entries)} contracts on Sourcify "
            f"(mainnet, solc >= {min_version})...",
            file=sys.stderr,
        )

        toml_doc = tomlkit.document()
        matched_count = 0
        for index, address_entry in enumerate(address_entries, start=1):
            address = address_entry.address
            print(
                f"  [{index}/{len(address_entries)}] {address} {address_entry.name}",
                file=sys.stderr,
            )

            try:
                entry = _process_contract(session, address_entry, address, min_version)
            except requests.exceptions.RequestException as e:
                print(f"    skip: HTTP error after retries ({e})", file=sys.stderr)
                continue

            if entry is None:
                continue

            (output_dir / f"{entry.bench_id}.json").write_text(
                json.dumps(entry.standard_json, indent=2)
            )
            toml_doc[entry.bench_id] = _entry_fields(entry, address_entry, address)

            matched_count += 1
            proxy_note = f" via proxy {address}" if entry.implementation_address else ""
            print(
                f"    matched: solc {entry.compiler_version}{proxy_note}",
                file=sys.stderr,
            )

    (output_dir / "benchmarks.toml").write_text(tomlkit.dumps(toml_doc))
    print(
        f"Wrote {matched_count} of {len(address_entries)} contracts to {output_dir}",
        file=sys.stderr,
    )
    return matched_count
