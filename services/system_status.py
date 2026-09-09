from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil


@dataclass(frozen=True)
class SystemStatus:
    timestamp: datetime
    temperature_c: Optional[float]
    cpu_percent: float
    cpu_count: int
    memory_used: int
    memory_total: int
    memory_percent: float
    storage_used: int
    storage_total: int
    storage_percent: float


def collect_system_status() -> SystemStatus:
    memory = psutil.virtual_memory()
    storage = psutil.disk_usage(Path.home().anchor or "/")
    return SystemStatus(
        timestamp=datetime.now().astimezone(),
        temperature_c=_read_cpu_temperature(),
        cpu_percent=psutil.cpu_percent(interval=0.1),
        cpu_count=psutil.cpu_count() or 1,
        memory_used=memory.used,
        memory_total=memory.total,
        memory_percent=memory.percent,
        storage_used=storage.used,
        storage_total=storage.total,
        storage_percent=storage.percent,
    )


def _read_cpu_temperature() -> Optional[float]:
    try:
        temperatures = psutil.sensors_temperatures(fahrenheit=False)
    except (AttributeError, OSError):
        return None

    preferred_groups = ("cpu_thermal", "coretemp", "k10temp", "acpitz")
    ordered_groups = [temperatures.get(name, []) for name in preferred_groups]
    ordered_groups.extend(
        readings for name, readings in temperatures.items() if name not in preferred_groups
    )
    for readings in ordered_groups:
        for reading in readings:
            if reading.current is not None:
                return float(reading.current)
    return None