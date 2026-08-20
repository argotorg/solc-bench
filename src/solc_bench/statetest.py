"""Run evmone-statetest against a fixture and verify its output against
independent mainnet ground truth embedded in the fixture's per-case
`post.<fork>[0]` object, alongside the ordinary `hash`/`logs`/`indexes`
expectation fields.

See `fixture_builder.py` for how fixtures are assembled from real mainnet
data (including that ground truth, via `fetch_required_status`/
`fetch_required_state_diff`/`fetch_required_logs_hash`) and finalized (via
`solve_expected_hashes`/`patch_expected_hashes`) into a form evmone-statetest
can load.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _hex32(n: int) -> str:
    """32-byte-padded hex, required for storage keys/values (evmc::from_hex<bytes32> is fixed-width)."""
    return f"0x{n:064x}"


@dataclass
class ReplayResult:
    passed: bool
    gas_used: int | None
    notes: list[str] = field(default_factory=list)


class ReplayMismatch(RuntimeError):
    """Raised by `verify_replay` when evmone's actual output doesn't match
    the fixture's ground truth. Carries the same `ReplayResult` a caller
    would get on success, via `.result`, so a caller that wants gas/status
    even when verification fails (e.g. `compiler_swap.py` comparing a
    candidate build that behaves differently) doesn't need a second
    evmone-statetest invocation to get it.
    """

    def __init__(self, message: str, result: ReplayResult):
        super().__init__(message)
        self.result = result


def verify_replay(
    fixture_path: Path,
    evmone_statetest_bin: Path,
    exempt_sender_balance: bool = False,
) -> ReplayResult:
    """Run `evmone-statetest` once, with both `--trace-summary` and
    `--dump-statediff`, and check its output against the fixture's ground
    truth from mainnet, read from the (single) case in `post.<fork>[0]`:

      1. Execution status (`requiredStatus`, see
         `fixture_builder.fetch_required_status`) - the replayed transaction
         must succeed or fail exactly as it did on mainnet, not merely
         succeed in isolation.
      2. Logs (`logs`, see `fixture_builder.fetch_required_logs_hash`) - the
         replayed transaction must emit exactly the same events. Unlike the
         state root (see below), logs encode observable contract behavior,
         not gas efficiency, so they're expected to match exactly even when
         comparing two different compiler builds of the same contract (see
         `exempt_sender_balance` and `compiler_swap.py`'s `compare_gas`).
      3. StateDiff (`requiredStateDiff`, see
         `fixture_builder.fetch_required_state_diff`) - independent,
         per-account, per-slot evidence the replay's actual effects are
         correct.

    The state root (`post.<fork>[0].hash`) is deliberately never checked
    here: it's part of `solve_expected_hashes`'s self-consistency mechanism,
    not independent ground truth, and it directly encodes the account's
    `code` field - swapping in a different compiler build's bytecode (see
    `compiler_swap.py`) necessarily changes it even when nothing else about
    the transaction's effects changed at all.

    The three flags/dumps used don't conflict with each other (unlike
    `--trace`, which `--trace-summary` excludes): the summary line
    (including `logsHash`) goes to stderr, the statediff line to stdout,
    both from the same run, so one subprocess call covers every check.

    `exempt_sender_balance`, when True, skips the *balance* field
    specifically for the transaction's own sender in the StateDiff check
    (`nonce`/`code` are still checked) - for comparing a different compiler
    build's bytecode against the same fixture, where the sender legitimately
    pays a different amount of gas, but nothing else about the transaction's
    real effects should change. The coinbase remains exempt from the
    StateDiff check entirely, regardless of this parameter (see below).

    Raises `ReplayMismatch` (a `RuntimeError` subclass, carrying a
    `ReplayResult` via `.result`) on a genuine mismatch in any check: a
    status/logs value that doesn't match mainnet's, a StateDiff value
    present in `required` that evmone computed differently, or a required
    account/slot missing from evmone's output entirely.

    The block's coinbase (`env.currentCoinbase`) is skipped entirely in the
    StateDiff check: every transaction credits it a fee, but that's standard,
    well-tested EVM bookkeeping unrelated to the contract under test - not
    worth checking for a value this verification doesn't actually care about.

    `ReplayResult.notes` collects informational notes for two known-benign
    StateDiff asymmetries that are NOT treated as failures (both make
    evmone's diff a superset of `required`, never a conflicting subset):
      - evmone additionally lists an account as modified whose nonce/balance
        didn't actually change (the caller merely touched it, e.g. was `to`
        of a CALL) - `state::State::build_diff` unconditionally reports
        nonce/balance for every touched account (see the TODO in
        `state.cpp`'s `build_diff`).
      - evmone additionally lists a storage slot cleared to exactly `0x0`
        that `required` doesn't mention - geth's own diffMode omits slots
        whose final value is zero ("don't include the empty slot", see
        `fixture_builder.fetch_required_state_diff`).

    Skips a given check if the fixture's case has no corresponding field to
    check it against (a fixture's `logs` is `"0x0"` only when
    `solve_expected_hashes` hasn't been run against it yet - not real
    ground truth, so that placeholder is treated the same as absent).
    """
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    (cases,) = data[test_name]["post"].values()
    case = cases[0]
    required_status = case.get("requiredStatus")
    required_diff = case.get("requiredStateDiff")
    required_logs = case.get("logs")
    if required_logs == "0x0":
        required_logs = None

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
    actual_success = bool(summary.get("pass"))
    gas_used = int(summary["gasUsed"], 16) if "gasUsed" in summary else None

    mismatches: list[str] = []
    notes: list[str] = []

    def norm(v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    if required_status is not None:
        required_success = required_status.lower() == "0x1"
        if required_success != actual_success:
            mismatches.append(
                f"transaction result: required status={required_status} "
                f"(success={required_success}), evmone success={actual_success} ({summary})"
            )

    if required_logs is not None:
        actual_logs = summary.get("logsHash")
        if norm(actual_logs) != norm(required_logs):
            mismatches.append(f"logs: required={required_logs} evmone={actual_logs}")

    if required_diff is not None:
        coinbase = data[test_name]["env"]["currentCoinbase"].lower()
        sender = data[test_name]["transaction"]["sender"].lower() if exempt_sender_balance else None

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
            for field_name in ("nonce", "balance", "code"):
                if field_name == "balance" and addr_l == sender:
                    continue
                if field_name in req_acc and norm(act_acc.get(field_name)) != norm(
                    req_acc[field_name]
                ):
                    mismatches.append(
                        f"account {addr} {field_name}: required={req_acc[field_name]} "
                        f"evmone={act_acc.get(field_name)}"
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

    replay_result = ReplayResult(passed=actual_success, gas_used=gas_used, notes=notes)

    if mismatches:
        raise ReplayMismatch(
            f"{fixture_path}: evmone's replay does not match mainnet ground truth:\n"
            + "\n".join(f"  - {m}" for m in mismatches),
            replay_result,
        )
    return replay_result
