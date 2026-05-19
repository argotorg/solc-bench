"""Detect host hardware + environment for benchmark result fingerprinting.

Self-describing benchmark results: every `bench-results.json` carries the
hardware and kernel posture it was measured on, so historical results can
be re-interpreted and `compare` can flag baseline/target mismatches.
"""

import os
import platform
import socket


def hardware() -> dict:
    """Return CPU model, vendor, max MHz, configured/online thread count, kernel, hostname."""
    model, vendor, configured_threads = _cpu_info()
    return {
        "cpu_model": model,
        "cpu_vendor": vendor,
        "cpu_max_mhz": _cpu_max_mhz(),
        "cpu_threads_configured": configured_threads,
        "cpu_threads_online": _online_threads(),
        "kernel": platform.release(),
        "hostname": socket.gethostname(),
    }


def _online_threads() -> int:
    """CPUs available to this process. Affinity-aware where the OS supports it.

    Prefer sched_getaffinity over process_cpu_count on Linux.
    See: https://docs.python.org/3/library/os.html#os.sched_getaffinity
    And: https://docs.python.org/3/library/os.html#os.process_cpu_count
    """
    if hasattr(os, "sched_getaffinity"):  # Linux
        return len(os.sched_getaffinity(0))
    if hasattr(os, "process_cpu_count"):  # macOS/Windows on Python 3.13+
        return os.process_cpu_count()
    return os.cpu_count() or 0


def environment() -> dict:
    """Return host configuration relevant to benchmark variance."""
    cmdline_tokens = set(_read_text("/proc/cmdline").split())
    return {
        "governor": _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "boost_enabled": _boost_enabled(),
        "mitigations_off": "mitigations=off" in cmdline_tokens,
        "kaslr_off": "nokaslr" in cmdline_tokens,
        "aslr": _read_text("/proc/sys/kernel/randomize_va_space"),
        "thp": _thp_setting(),
        "smt_active": _read_text("/sys/devices/system/cpu/smt/active") == "1",
    }


def check_variance_factors(env: dict | None = None) -> list[str]:
    """Return list of human-readable warnings about benchmark conditions.

    Each entry is a string ready to be printed. Empty list means the host
    is configured for low-variance benchmarking.
    """
    if env is None:
        env = environment()

    warnings = []
    if env["governor"] and env["governor"] != "performance":
        warnings.append(
            f"CPU governor is '{env['governor']}', not 'performance'. "
            "Wall-time and cycles will be noisier."
        )
    if env["thp"] == "always":
        warnings.append(
            "Transparent huge pages set to 'always'. Background khugepaged "
            "compaction can cause latency spikes."
        )
    if env["smt_active"]:
        warnings.append(
            "SMT is enabled. Single-thread variance is higher than with "
            "the SMT siblings of measurement CPUs offline."
        )
    if env["aslr"] and env["aslr"] != "0":
        warnings.append(
            "Address space randomization (ASLR) is enabled. Run-to-run "
            "memory layout differs; expect noisier wall_time."
        )
    if env["boost_enabled"] is True:
        warnings.append(
            "CPU boost is enabled. Clock speed varies under load, hurting "
            "reproducibility. Consider disabling for benchmark hosts."
        )
    return warnings


def _cpu_info() -> tuple[str, str, int]:
    """Return (model_name, vendor_id, processor_count) from /proc/cpuinfo."""
    model = ""
    vendor = ""
    threads = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                key = key.strip()
                value = value.strip()
                if key == "model name" and not model:
                    model = value
                elif key == "vendor_id" and not vendor:
                    vendor = value
                elif key == "processor":
                    threads += 1
    except OSError:
        pass
    return model, vendor, threads


def _cpu_max_mhz() -> float:
    """Read the CPU's maximum capable frequency from sysfs (in MHz).

    See: https://www.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html
    """
    raw = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        return float(raw) / 1000.0  # kHz -> MHz
    except ValueError:
        return 0.0


def _read_text(path: str, bufsize: int = 256) -> str:
    """Read up to `bufsize` bytes from `path` as UTF-8 text. Returns "" on failure."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            data = os.read(fd, bufsize)
        finally:
            os.close(fd)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def _boost_enabled() -> bool | None:
    """Return True/False if we can detect boost state, None if undetectable."""
    boost = _read_text("/sys/devices/system/cpu/cpufreq/boost")
    if boost:
        return boost == "1"
    no_turbo = _read_text("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if no_turbo:
        return no_turbo == "0"
    return None


def _thp_setting() -> str:
    """Read the active THP setting (the [bracketed] value)."""
    raw = _read_text("/sys/kernel/mm/transparent_hugepage/enabled")
    for token in raw.split():
        if token.startswith("[") and token.endswith("]"):
            return token[1:-1]
    return ""
