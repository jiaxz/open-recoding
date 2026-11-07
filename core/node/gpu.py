"""NVIDIA compute device discovery and mounts for the sandbox adapter only."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat


def usable_device(path):
    try:
        return (not path.is_symlink() and stat.S_ISCHR(path.stat().st_mode)
                and os.access(path, os.R_OK | os.W_OK))
    except OSError:
        return False


@dataclass(frozen=True)
class GPUConfig:
    status: str
    devices: tuple[str, ...] = ()
    gpu_devices: tuple[str, ...] = ()
    read_only_paths: tuple[str, ...] = ()
    cuda_home: str | None = None
    visible_devices: str | None = None
    device_order: str | None = None

    def metadata(self):
        return {"status": self.status, "enabled": self.status == "enabled",
                "backend": "nvidia", "devices": list(self.devices),
                "device_count": len(self.gpu_devices), "cuda_home": self.cuda_home,
                "cuda_visible_devices": self.visible_devices}

    def mount_arguments(self):
        arguments = []
        for path in self.devices:
            arguments += ["--dev-bind", path, path]
        for path in self.read_only_paths:
            arguments += ["--ro-bind", path, path]
        return arguments

    def environment(self):
        if self.status != "enabled":
            return {"CUDA_VISIBLE_DEVICES": "-1"}
        environment = {}
        if self.visible_devices is not None:
            environment["CUDA_VISIBLE_DEVICES"] = self.visible_devices
        if self.device_order is not None:
            environment["CUDA_DEVICE_ORDER"] = self.device_order
        if self.cuda_home:
            environment["CUDA_HOME"] = self.cuda_home
            environment["CUDA_PATH"] = self.cuda_home
            libraries = Path(self.cuda_home) / "lib64"
            if libraries.is_dir():
                # Never forward the host's arbitrary LD_LIBRARY_PATH.
                environment["LD_LIBRARY_PATH"] = str(libraries)
        return environment


def discover_gpu(*, enabled=True):
    """Expose accessible NVIDIA compute nodes, never the host's entire /dev."""
    if not enabled:
        return GPUConfig("disabled")
    gpu_devices = tuple(str(path) for path in sorted(Path("/dev").glob("nvidia[0-9]*"))
                        if re.fullmatch(r"nvidia[0-9]+", path.name) and usable_device(path))
    control = Path("/dev/nvidiactl")
    if not gpu_devices or not usable_device(control):
        return GPUConfig("unavailable")
    devices = (*gpu_devices, str(control), *(str(path) for path in
                (Path("/dev/nvidia-uvm"), Path("/dev/nvidia-uvm-tools")) if usable_device(path)))
    # CUDA/NVML inspect sysfs before opening the already-loaded driver. Without
    # these read-only paths, initialization can fail with CUDA error 304.
    # No writable driver controls, host /dev directory or host process tree.
    metadata = tuple(str(path) for path in (Path("/proc/driver/nvidia/version"),
                     Path("/proc/driver/nvidia/params"), Path("/proc/driver/nvidia/gpus"),
                     Path("/sys/devices"), Path("/sys/bus/pci"),
                     Path("/sys/module/nvidia"), Path("/sys/module/nvidia_uvm"))
                     if path.exists())
    cuda = Path("/usr/local/cuda").resolve()
    cuda_home = str(cuda) if cuda.is_relative_to("/usr") and cuda.is_dir() else None
    order = os.environ.get("CUDA_DEVICE_ORDER")
    return GPUConfig("enabled", devices, gpu_devices, metadata, cuda_home,
                     os.environ.get("CUDA_VISIBLE_DEVICES"),
                     order if order in {"PCI_BUS_ID", "FASTEST_FIRST"} else None)


def gpu_description(metadata):
    """Plain data for callers to display; no terminal or core dependencies."""
    if metadata.get("status") == "disabled":
        return "off (--no-gpu)"
    if not metadata.get("enabled"):
        return "unavailable (no accessible NVIDIA devices)"
    description = f"NVIDIA · {metadata['device_count']} device(s) exposed"
    visible = metadata.get("cuda_visible_devices")
    if visible is not None:
        description += f" · CUDA_VISIBLE_DEVICES={visible!r}"
    return description
