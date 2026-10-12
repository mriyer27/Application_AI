"""Best-effort hardware detection and local-model recommendations.

Detection never raises: anything that cannot be determined is reported as
unknown / none. GPU detection only recognizes accelerators Ollama uses out of
the box (NVIDIA via ``nvidia-smi``, AMD via ``rocm-smi``, Apple Silicon);
integrated laptop graphics (e.g. Radeon Vega in a Ryzen 7 5800U) are treated
as "no dedicated GPU" because Ollama runs on the CPU there.

Recommendations are a starting point. Use ``job-agent llm benchmark`` to
MEASURE speed and JSON validity on your own machine before deciding anything
(including whether a hardware upgrade is worth it).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GB = 1024**3


@dataclass(frozen=True)
class Hardware:
    total_ram_gb: float
    cpu_count: int  # logical CPUs
    cpu_model: str
    gpu: str | None  # dedicated accelerator usable by Ollama, best effort; None = none detected
    os: str  # platform.system(): Linux | Darwin | Windows
    physical_cores: int | None = None
    available_ram_gb: float | None = None
    gpu_vram_gb: float | None = None
    arch: str = ""
    apple_silicon: bool = False

    def summary(self) -> str:
        ram = f"{self.total_ram_gb:.1f} GB RAM"
        if self.available_ram_gb is not None:
            ram += f" ({self.available_ram_gb:.1f} GB available)"
        cores = f"{self.cpu_count} logical CPUs"
        if self.physical_cores:
            cores += f"/{self.physical_cores} cores"
        if self.gpu:
            gpu = f"GPU: {self.gpu}" + (f" ({self.gpu_vram_gb:.0f} GB VRAM)" if self.gpu_vram_gb else "")
        else:
            gpu = "no dedicated GPU detected (CPU inference)"
        os_name = {"Darwin": "macOS"}.get(self.os, self.os)
        return f"{ram}, {cores} ({self.cpu_model}), {gpu}, {os_name}{' ' + self.arch if self.arch else ''}"


@dataclass(frozen=True)
class ModelRecommendation:
    name: str
    approx_size_gb: float  # download / weights size (default Q4_K_M quantization)
    role: str  # default | fast | quality | avoid
    why: str


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run a fixed, argument-list command (no shell). Returns stdout or None."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _ram_linux() -> tuple[float | None, float | None]:
    text = _read("/proc/meminfo")
    if not text:
        return None, None
    values = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            values[m.group(1)] = int(m.group(2)) * 1024
    total = values.get("MemTotal")
    avail = values.get("MemAvailable")
    return (total / _GB if total else None), (avail / _GB if avail else None)


def _ram_windows() -> tuple[float | None, float | None]:  # pragma: no cover - Windows only
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return stat.ullTotalPhys / _GB, stat.ullAvailPhys / _GB
    except (AttributeError, OSError):
        pass
    return None, None


def _ram(system: str) -> tuple[float | None, float | None]:
    if system == "Linux":
        total, avail = _ram_linux()
    elif system == "Darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        total, avail = (int(raw) / _GB if raw and raw.isdigit() else None), None
    elif system == "Windows":
        total, avail = _ram_windows()
    else:
        total, avail = None, None
    if total is None:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / _GB
        except (AttributeError, ValueError, OSError):
            total = None
    return total, avail


def _cpu_linux() -> tuple[str | None, int | None]:
    text = _read("/proc/cpuinfo")
    if not text:
        return None, None
    model = None
    cores: set[tuple[str, str]] = set()
    phys, core = "0", None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "model name" and model is None:
            model = value
        elif key == "physical id":
            phys = value
        elif key == "core id":
            core = value
            cores.add((phys, core))
    return model, (len(cores) or None)


def _cpu_windows() -> str | None:  # pragma: no cover - Windows only
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (ImportError, OSError):
        return None


def _cpu(system: str) -> tuple[str, int | None]:
    model: str | None = None
    physical: int | None = None
    if system == "Linux":
        model, physical = _cpu_linux()
    elif system == "Darwin":
        model = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        raw = _run(["sysctl", "-n", "hw.physicalcpu"])
        physical = int(raw) if raw and raw.isdigit() else None
    elif system == "Windows":
        model = _cpu_windows()
    return (model or platform.processor() or "unknown CPU"), physical


def _gpu(system: str, arch: str) -> tuple[str | None, float | None, bool]:
    if system == "Darwin" and arch in {"arm64", "aarch64"}:
        return "Apple Silicon GPU (unified memory)", None, True
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if out:
        first = out.splitlines()[0]
        name, _, mem = first.partition(",")
        try:
            vram = float(mem.strip()) / 1024
        except ValueError:
            vram = None
        return f"NVIDIA {name.strip()}".replace("NVIDIA NVIDIA", "NVIDIA"), vram, False
    if _run(["rocm-smi", "--showproductname"]):
        return "AMD GPU (ROCm)", None, False
    return None, None, False


def detect_hardware() -> Hardware:
    system = platform.system() or "unknown"
    arch = platform.machine() or ""
    total, avail = _ram(system)
    cpu_model, physical = _cpu(system)
    gpu, vram, apple = _gpu(system, arch)
    return Hardware(
        total_ram_gb=round(total, 1) if total else 0.0,
        cpu_count=os.cpu_count() or 0,
        cpu_model=cpu_model,
        gpu=gpu,
        os=system,
        physical_cores=physical,
        available_ram_gb=round(avail, 1) if avail is not None else None,
        gpu_vram_gb=round(vram, 1) if vram else None,
        arch=arch,
        apple_silicon=apple,
    )


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
#: Approximate download sizes (Ollama library default Q4_K_M quantization).
MODEL_SIZES_GB: dict[str, float] = {
    "qwen2.5:1.5b-instruct": 1.0,
    "qwen2.5:3b-instruct": 1.9,
    "llama3.2:3b": 2.0,
    "qwen2.5:7b-instruct": 4.7,
    "qwen2.5:14b-instruct": 9.0,
    "qwen2.5:32b-instruct": 20.0,
}


def _rec(name: str, role: str, why: str) -> ModelRecommendation:
    return ModelRecommendation(name=name, approx_size_gb=MODEL_SIZES_GB[name], role=role, why=why)


def recommend_models(hw: Hardware) -> list[ModelRecommendation]:
    """Ordered recommendations; the first entry is the suggested default (``llm.model``)."""
    ram = hw.total_ram_gb or 0.0
    vram = hw.gpu_vram_gb or 0.0
    big_gpu = (hw.gpu is not None and vram >= 12) or (hw.apple_silicon and ram >= 24)
    gpu_fits_7b = (hw.gpu is not None and vram >= 6) or (hw.apple_silicon and ram >= 12)

    if ram and ram < 6:
        return [
            _rec("qwen2.5:1.5b-instruct", "default",
                 "Only small models fit in under 6 GB RAM next to the OS and a browser; expect weaker "
                 "instruction following, so every output is validated and falls back to rules."),
            _rec("qwen2.5:3b-instruct", "avoid",
                 "~1.9 GB weights plus context may cause swapping on this machine."),
            _rec("qwen2.5:7b-instruct", "avoid", "Does not fit comfortably in this much RAM."),
        ]
    if ram and ram < 11.5:
        return [
            _rec("qwen2.5:3b-instruct", "default",
                 "Best balance for ~8 GB RAM: ~1.9 GB, follows JSON schemas reliably enough for validated "
                 "extraction, roughly 2-3x faster than 7B on CPU."),
            _rec("qwen2.5:1.5b-instruct", "fast", "Fastest option; noticeably more dropped/invalid fields."),
            _rec("qwen2.5:7b-instruct", "avoid",
                 "~4.7 GB plus context alongside a browser on ~8 GB RAM is likely to swap; only try it with "
                 "other apps closed and measure with `job-agent llm benchmark`."),
            _rec("qwen2.5:14b-instruct", "avoid", ">= 13B models need more memory than this machine has."),
        ]

    recs: list[ModelRecommendation] = []
    if gpu_fits_7b:
        recs.append(_rec("qwen2.5:7b-instruct", "default",
                         "Fits fully in GPU memory: reliable JSON / instruction following at interactive speed."))
    else:
        recs.append(_rec("qwen2.5:7b-instruct", "default",
                         "Most reliable JSON and instruction following that fits a CPU-only 16 GB laptop "
                         "(~4.7 GB, Q4_K_M) with room for a browser; expect very roughly 4-8 tokens/s "
                         "generation on an 8-core laptop CPU, i.e. about 1-3 minutes per job analysis."))
    recs.append(_rec("qwen2.5:3b-instruct", "fast",
                     "~1.9 GB, roughly 2-3x faster on CPU; good for email classification and quick checks, "
                     "slightly more skills dropped by validation."))
    recs.append(_rec("llama3.2:3b", "fast",
                     "~2.0 GB alternative fast model; similar speed to qwen2.5:3b, a little weaker at strict JSON."))
    if big_gpu or (ram >= 24 and hw.gpu is None):
        recs.append(_rec("qwen2.5:14b-instruct", "quality",
                         "Higher quality analysis; " + ("fits in GPU memory." if big_gpu else
                                                        "on CPU about 2x slower than 7B, so use it only for "
                                                        "small batches after measuring.")))
        recs.append(_rec("qwen2.5:32b-instruct", "quality" if vram >= 24 else "avoid",
                         "~20 GB; too slow on CPU and only practical with >= 24 GB of GPU memory."))
    else:
        recs.append(_rec("qwen2.5:14b-instruct", "avoid",
                         "Avoid >= 13B models on a CPU-only 16 GB machine: ~9 GB weights plus context leaves "
                         "too little memory for a browser and runs at only ~2-3 tokens/s."))
    return recs


RUNS_VS_RELIABLE = """\
A model that *runs* is not the same as a model that is *reliable* for job analysis:
- JSON validity: the reply must parse and match the schema every time (Ollama's structured outputs help; small
  models still produce empty or truncated fields more often).
- Instruction following: e.g. "copy sponsorship sentences verbatim" and "only list skills written in the
  posting". Validation drops violations, so a weaker model silently gives you *less* information.
- Hallucination rate: how often it invents skills, years or requirements (all dropped by deterministic checks,
  but frequent drops mean poorer analysis).
- Context length: a long posting plus the prompt is ~3-5k tokens; llm.num_ctx (default 8192) must hold it, and
  larger contexts cost RAM and time.
- Speed: CPU generation of a few tokens per second means minutes per job; the scheduler only analyzes the top
  scoring.max_llm_jobs_per_run jobs per run for this reason.
Measure with `job-agent llm benchmark --model <name>` (tokens/s, load time, JSON validity, grounding) before
switching models or buying hardware."""
