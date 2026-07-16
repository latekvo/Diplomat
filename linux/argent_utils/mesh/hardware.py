"""Machine-strength auto-detection from local hardware specs.

The mesh ranks nodes by *strength* (tier 1 = strongest) so ``weakest-first``
routing keeps a powerful interactive machine free. Rather than make the operator
guess a number, a fresh node measures the box it runs on and maps that to a 1..5
tier (:func:`argent_utils.mesh.identity.load`). A manual edit in the panel pins
the tier and turns auto-detection off (``strengthAuto=False``).

The score is CPU-quality-first: what the mesh routes is agent work (builds,
tests, tool calls), whose wall-clock tracks per-core speed far more than spec-
sheet totals. Raw RAM gigabytes and logical thread counts are terrible
cross-ISA comparators — an SMT x86 laptop reports twice its real cores and can
carry 64 GB while being much slower than an Apple-Silicon box that reports
"only" 24 GB of unified memory. So:

- **CPU class** (0..4, dominant): Apple-Silicon variant (Pro/Max/Ultra > base M)
  on Macs; top boost clock as a rough per-core proxy elsewhere.
- **Physical cores** (0..2): real cores, never SMT threads.
- **RAM** (0..1): a has-enough-for-parallel-work check, not a strength axis.
- **Discrete GPU** (0..1): NVIDIA/AMD only — an Apple GPU is already priced
  into the chip class.

Stdlib-only and best-effort on both platforms the node runs on (Linux + macOS);
every probe degrades to a neutral value on failure, so detection never raises and
an undetectable box lands on the ``tiers.default`` from ``core/mesh.json``.
"""

from __future__ import annotations

import glob
import os
import platform as _platform
import subprocess

from . import config

# Top of the strength_score scale: cpu class 4 + cores 2 + ram 1 + dgpu 1.
_MAX_SCORE = 8


def total_ram_gb() -> float | None:
    """Physical RAM in GiB, or None if it can't be read."""
    system = _platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = float(line.split()[1])  # value is in kB
                        return kb / (1024.0 * 1024.0)
        elif system == "Darwin":
            out = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2.0,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip()) / (1024.0 ** 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def cpu_cores() -> int | None:
    """Logical CPU count, or None."""
    try:
        return os.cpu_count()
    except Exception:  # noqa: BLE001 — cpu_count is documented not to raise, but be safe
        return None


def physical_cpu_cores() -> int | None:
    """Real core count — SMT threads deliberately excluded (a 16-thread laptop
    has 8 cores' worth of throughput, and counting threads is exactly how mid
    x86 boxes used to out-score Apple Silicon). Falls back to the logical count
    where physical topology is unreadable (e.g. ARM Linux, where they're equal
    anyway)."""
    system = _platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(  # noqa: S603,S607
                ["sysctl", "-n", "hw.physicalcpu"],
                capture_output=True, text=True, timeout=2.0,
            )
            if out.returncode == 0 and out.stdout.strip():
                return int(out.stdout.strip())
        elif system == "Linux":
            cores: set[tuple[str, str]] = set()
            block: dict[str, str] = {}
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in list(fh) + [""]:  # sentinel flushes the last block
                    line = line.strip()
                    if not line:
                        if "physical id" in block and "core id" in block:
                            cores.add((block["physical id"], block["core id"]))
                        block = {}
                        continue
                    key, _, value = line.partition(":")
                    block[key.strip()] = value.strip()
            if cores:
                return len(cores)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return cpu_cores()


def apple_chip() -> str | None:
    """The Apple-Silicon chip name (e.g. "Apple M4 Pro"), or None on anything
    else (Linux, Intel Macs, unreadable)."""
    if _platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(  # noqa: S603,S607
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=2.0,
        )
        name = out.stdout.strip()
        if out.returncode == 0 and name.startswith("Apple "):
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def max_cpu_ghz() -> float | None:
    """Top boost clock in GHz — the rough per-core-quality proxy for non-Apple
    CPUs (IPC differences are invisible to a spec probe, but a 5 GHz part is
    reliably a stronger per-core bin than a 3.5 GHz one). None when unreadable."""
    system = _platform.system()
    try:
        if system == "Linux":
            freqs = []
            for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq"):
                try:
                    with open(path, encoding="utf-8") as fh:
                        freqs.append(float(fh.read().strip()))  # kHz
                except (OSError, ValueError):
                    continue
            if freqs:
                return max(freqs) / 1e6
        elif system == "Darwin":
            out = subprocess.run(  # noqa: S603,S607 — Intel Macs only; absent on Apple Silicon
                ["sysctl", "-n", "hw.cpufrequency_max"],
                capture_output=True, text=True, timeout=2.0,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip()) / 1e9  # Hz
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def has_discrete_gpu() -> bool:
    """Best-effort: does this box have a discrete GPU (a rough proxy for real
    compute headroom)? NVIDIA/AMD only — Apple GPUs are integrated and already
    credited via the chip class. Never raises; a False negative just nudges the
    tier one step softer, which is harmless."""
    system = _platform.system()
    try:
        if system == "Linux":
            drm = "/sys/class/drm"
            if os.path.isdir(drm):
                for name in os.listdir(drm):
                    # cardN (not cardN-<connector>) with a vendor we recognise as a
                    # dGPU. Integrated Intel graphics also expose a card, so key on
                    # NVIDIA/AMD vendor ids, which are effectively always discrete here.
                    if not (name.startswith("card") and "-" not in name):
                        continue
                    vendor_path = os.path.join(drm, name, "device", "vendor")
                    try:
                        with open(vendor_path, encoding="utf-8") as fh:
                            vendor = fh.read().strip().lower()
                    except OSError:
                        continue
                    if vendor in ("0x10de", "0x1002"):  # NVIDIA, AMD
                        return True
            return os.path.exists("/proc/driver/nvidia/version")
        if system == "Darwin":
            out = subprocess.run(  # noqa: S603,S607
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=6.0,
            )
            if out.returncode == 0:
                text = out.stdout.lower()
                return any(k in text for k in ("radeon", "amd", "nvidia"))
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def cpu_class(chip: str | None, boost_ghz: float | None) -> int:
    """Per-core CPU quality, 0..4. Pure, so the thresholds are unit-testable.

    Apple Silicon tops the scale outright: every M-series part is a leading
    per-core performer, and the Pro/Max/Ultra bins add sustained-multicore
    headroom. Elsewhere the boost clock buckets parts coarsely (high-bin
    desktop/HX silicon vs. mainstream vs. old-or-small)."""
    if chip:
        return 4 if any(s in chip for s in ("Pro", "Max", "Ultra")) else 3
    if boost_ghz is not None:
        if boost_ghz >= 4.8:
            return 2
        if boost_ghz >= 4.0:
            return 1
    return 0


def strength_score(ram_gb: float | None, cores: int | None, dgpu: bool,
                   cpu: int = 0) -> int:
    """Combine specs into a 0..8 strength score (higher = stronger). Pure, so the
    thresholds are unit-testable without touching the machine.

    ``cpu`` is the :func:`cpu_class` result and dominates; ``cores`` are
    PHYSICAL cores. RAM is a single is-there-enough point — spec-sheet gigabytes
    say little about speed, and letting them dominate is how a 64 GB mid laptop
    used to outrank an M-series Pro box."""
    score = cpu
    # Physical cores: throughput for concurrent jobs.
    if cores is not None:
        if cores >= 12:
            score += 2
        elif cores >= 8:
            score += 1
    # RAM: enough headroom for parallel agent work?
    if ram_gb is not None and ram_gb >= 16:
        score += 1
    # A discrete GPU still marks a workstation-class box.
    if dgpu:
        score += 1
    return min(score, _MAX_SCORE)


def _score_to_tier(score: int, lo: int, hi: int) -> int:
    """Map the 0.._MAX_SCORE score onto the [lo, hi] tier scale (1 = strongest,
    so a high score yields a low tier number)."""
    # score 8→tier 1 (strongest) … score 0→tier 5 (weakest), clamped to bounds.
    tier = hi - round(score / _MAX_SCORE * (hi - lo))
    return max(lo, min(hi, tier))


def detect_tier() -> int:
    """This machine's auto-detected strength tier (``tiers.min``..``tiers.max``,
    1 = strongest). ``ARGENT_MESH_TIER`` forces a value (tests / manual pinning at
    the process level); an undetectable box falls back to ``tiers.default``."""
    lo, hi, default = config.tier_bounds()
    forced = os.environ.get("ARGENT_MESH_TIER")
    if forced is not None:
        try:
            return max(lo, min(hi, int(forced)))
        except ValueError:
            pass
    chip = apple_chip()
    ram = total_ram_gb()
    cores = physical_cpu_cores()
    if ram is None and cores is None and chip is None:
        return default  # nothing to go on — neutral
    # Apple Silicon can't take a discrete GPU and needs no clock probe — skip
    # both (system_profiler alone costs ~1s on the identity-load hot path).
    dgpu = False if chip else has_discrete_gpu()
    ghz = None if chip else max_cpu_ghz()
    return _score_to_tier(strength_score(ram, cores, dgpu, cpu_class(chip, ghz)), lo, hi)
