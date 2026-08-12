"""Run evmone-statetest against a fixture and verify its output against
independent mainnet ground truth embedded in the fixture's per-case
`post.<fork>[0]` object, alongside the ordinary `hash`/`logs`/`indexes`
expectation fields.

See `fixture_builder.py` for how fixtures are assembled from real mainnet
data (including that ground truth, via `fetch_required_status`/
`fetch_required_state_diff`) and finalized (via `solve_expected_hashes`/
`patch_expected_hashes`) into a form evmone-statetest can load.
"""

import json
import subprocess
from pathlib import Path
from typing import Any


def _hex32(n: int) -> str:
    """32-byte-padded hex, required for storage keys/values (evmc::from_hex<bytes32> is fixed-width)."""
    return f"0x{n:064x}"


def verify_replay(fixture_path: Path, evmone_statetest_bin: Path) -> list[str]:
    """Run `evmone-statetest` once, with both `--trace-summary` and
    `--dump-statediff`, and check its output against the fixture's ground
    truth from mainnet, read from the (single) case in `post.<fork>[0]`:

      1. Execution status (`requiredStatus`, see
         `fixture_builder.fetch_required_status`) - the replayed transaction
         must succeed or fail exactly as it did on mainnet, not merely
         succeed in isolation.
      2. StateDiff (`requiredStateDiff`, see
         `fixture_builder.fetch_required_state_diff`) - independent,
         per-account, per-slot evidence the replay's actual effects are
         correct.

    The two flags don't conflict (unlike `--trace`, which `--trace-summary`
    excludes): the summary line goes to stderr, the statediff line to stdout,
    from the same run, so one subprocess call covers both checks.

    Raises RuntimeError on a genuine mismatch in either check: a status that
    doesn't match mainnet's, or a value present in `required` that evmone
    computed differently, or a required account/slot missing from evmone's
    output entirely.

    The block's coinbase (`env.currentCoinbase`) is skipped entirely in the
    StateDiff check: every transaction credits it a fee, but that's standard,
    well-tested EVM bookkeeping unrelated to the contract under test - not
    worth checking for a value this verification doesn't actually care about.

    Returns a list of informational notes for two known-benign StateDiff
    asymmetries that are NOT treated as failures (both make evmone's diff a
    superset of `required`, never a conflicting subset):
      - evmone additionally lists an account as modified whose nonce/balance
        didn't actually change (the caller merely touched it, e.g. was `to`
        of a CALL) - `state::State::build_diff` unconditionally reports
        nonce/balance for every touched account (see the TODO in
        `state.cpp`'s `build_diff`).
      - evmone additionally lists a storage slot cleared to exactly `0x0`
        that `required` doesn't mention - geth's own diffMode omits slots
        whose final value is zero ("don't include the empty slot", see
        `fixture_builder.fetch_required_state_diff`).
    Skips a check (and, for StateDiff, its notes) if the fixture's case has
    no corresponding field to check it against.
    """
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    (cases,) = data[test_name]["post"].values()
    case = cases[0]
    required_status = case.get("requiredStatus")
    required_diff = case.get("requiredStateDiff")
    if required_status is None and required_diff is None:
        return []

    result = subprocess.run(
        [str(evmone_statetest_bin), str(fixture_path), "--trace-summary", "--dump-statediff"],
        capture_output=True,
        text=True,
    )
    # The trace-summary line goes to std::clog (stderr); the statediff line to stdout.
    summary_line = next(
        (line for line in result.stderr.splitlines() if line.startswith("{")), None
    )
    diff_line = next((line for line in result.stdout.splitlines() if line.startswith("{")), None)
    if summary_line is None or diff_line is None:
        raise RuntimeError(
            f"{fixture_path}: missing --trace-summary/--dump-statediff output:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    summary = json.loads(summary_line)
    actual_diff = json.loads(diff_line)

    mismatches: list[str] = []
    notes: list[str] = []

    if required_status is not None:
        required_success = required_status.lower() == "0x1"
        actual_success = bool(summary.get("pass"))
        if required_success != actual_success:
            mismatches.append(
                f"transaction result: required status={required_status} "
                f"(success={required_success}), evmone success={actual_success} ({summary})"
            )

    if required_diff is not None:
        coinbase = data[test_name]["env"]["currentCoinbase"].lower()

        def norm(v: Any) -> Any:
            return v.lower() if isinstance(v, str) else v

        for addr, req_acc in required_diff["modifiedAccounts"].items():
            addr_l = addr.lower()
            if addr_l == coinbase:
                continue
            act_acc = actual_diff["modifiedAccounts"].get(addr_l)
            if act_acc is None:
                mismatches.append(
                    f"account {addr}: required as modified, missing from evmone's diff"
                )
                continue
            for field in ("nonce", "balance", "code"):
                if field in req_acc and norm(act_acc.get(field)) != norm(req_acc[field]):
                    mismatches.append(
                        f"account {addr} {field}: required={req_acc[field]} "
                        f"evmone={act_acc.get(field)}"
                    )
            for slot, val in req_acc["modifiedStorage"].items():
                act_val = act_acc["modifiedStorage"].get(slot.lower())
                if act_val is None:
                    mismatches.append(f"account {addr} slot {slot}: required={val} evmone=missing")
                elif norm(act_val) != norm(val):
                    mismatches.append(f"account {addr} slot {slot}: required={val} evmone={act_val}")

        required_addrs = {a.lower() for a in required_diff["modifiedAccounts"]}
        for addr, act_acc in actual_diff["modifiedAccounts"].items():
            if addr == coinbase:
                continue
            if addr not in required_addrs:
                notes.append(
                    f"account {addr}: evmone reports modified, absent from required (touch-only?)"
                )
                continue
            req_storage = {
                k.lower() for k in required_diff["modifiedAccounts"][addr]["modifiedStorage"]
            }
            for slot, val in act_acc["modifiedStorage"].items():
                if slot not in req_storage and norm(val) == norm(_hex32(0)):
                    notes.append(
                        f"account {addr} slot {slot}: evmone reports clearing to 0, "
                        "omitted from required (geth diffMode convention)"
                    )

        required_deleted = {a.lower() for a in required_diff["deletedAccounts"]} - {coinbase}
        actual_deleted = {a.lower() for a in actual_diff["deletedAccounts"]} - {coinbase}
        if required_deleted != actual_deleted:
            mismatches.append(
                f"deletedAccounts: required={sorted(required_deleted)} evmone={sorted(actual_deleted)}"
            )

    if mismatches:
        raise RuntimeError(
            f"{fixture_path}: evmone's replay does not match mainnet ground truth:\n"
            + "\n".join(f"  - {m}" for m in mismatches)
        )
    return notes
