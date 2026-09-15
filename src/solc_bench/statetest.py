"""Run evmone-statetest against a fixture and verify its output against
the real mainnet status/logs/state-diff embedded in it (see `add_benchmark/fixture_builder.py`).
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _hex32(n: int) -> str:
    return f"0x{n:064x}"


@dataclass
class ReplayResult:
    gas_used: int | None
    notes: list[str] = field(default_factory=list)


class ReplayMismatch(RuntimeError):
    """Raised by `fixture_replay` on a ground-truth mismatch. Carries the
    same `ReplayResult` a caller would get on success, via `.result`."""

    def __init__(self, message: str, result: ReplayResult):
        super().__init__(message)
        self.result = result


def fixture_replay(
    fixture_path: Path,
    evmone_statetest_bin: Path,
    exempt_sender_balance: bool = False,
) -> ReplayResult:
    """Run evmone-statetest and check its output against the fixture's
    requiredStatus/logs/requiredStateDiff - never the state root.

    `exempt_sender_balance` skips only the sender's balance in the diff
    check, for comparing a different compiler build's bytecode.

    Raises `ReplayMismatch` on any genuine mismatch.
    """
    data = json.loads(fixture_path.read_text())
    (test_name,) = data.keys()
    (cases,) = data[test_name]["post"].values()
    case = cases[0]
    required_status = case.get("requiredStatus")
    required_diff = case.get("requiredStateDiff")
    required_logs = case.get("logs")

    result = subprocess.run(
        [str(evmone_statetest_bin), str(fixture_path), "--trace-summary", "--dump-statediff"],
        capture_output=True,
        text=True,
    )
    # the trace-summary line goes to stderr, the statediff line to stdout
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

    def is_empty_touch(acc: dict[str, Any]) -> bool:
        """A required "modified" entry for an account that never held any
        real state - nonce/balance/code all zero, no storage. geth's
        diffMode reports these as zero-valued modifications; evmone
        (correctly) reports them as pruned under EIP-161 instead."""
        return (
            norm(acc.get("nonce")) in (None, norm("0x0"))
            and norm(acc.get("balance")) in (None, norm("0x0"))
            and norm(acc.get("code")) in (None, norm("0x"))
            and not acc.get("modifiedStorage")
        )

    if required_diff is not None:
        coinbase = data[test_name]["env"]["currentCoinbase"].lower()
        sender = data[test_name]["transaction"]["sender"].lower() if exempt_sender_balance else None
        actual_deleted_l = {a.lower() for a in actual_diff["deletedAccounts"]}
        empty_touch_deleted: set[str] = set()

        for addr, req_acc in required_diff["modifiedAccounts"].items():
            addr_l = addr.lower()
            if addr_l == coinbase:
                continue
            act_acc = actual_diff["modifiedAccounts"].get(addr_l)
            if act_acc is None:
                if addr_l in actual_deleted_l and is_empty_touch(req_acc):
                    empty_touch_deleted.add(addr_l)
                    notes.append(
                        f"account {addr}: required as a zero-valued touch, evmone reports it "
                        "pruned instead (geth diffMode convention)"
                    )
                else:
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

        # evmone can legitimately report more than `required` - note it,
        # don't treat it as a mismatch.
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
        actual_deleted = actual_deleted_l - {coinbase} - empty_touch_deleted
        if required_deleted != actual_deleted:
            mismatches.append(
                f"deletedAccounts: required={sorted(required_deleted)} evmone={sorted(actual_deleted)}"
            )

    replay_result = ReplayResult(gas_used=gas_used, notes=notes)

    if mismatches:
        raise ReplayMismatch(
            f"{fixture_path}: evmone's replay does not match mainnet ground truth:\n"
            + "\n".join(f"  - {m}" for m in mismatches),
            replay_result,
        )
    return replay_result
