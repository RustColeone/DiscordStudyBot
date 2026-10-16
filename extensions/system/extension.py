"""System extension: host status (ported from services/system_status.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import psutil

from contracts import Capability, Contributions, Extension, Risk, ToolResult


def temperature() -> float | None:
    try:
        readings = psutil.sensors_temperatures(fahrenheit=False)
    except (AttributeError, OSError):
        return None
    preferred = ("cpu_thermal", "coretemp", "k10temp", "acpitz")
    groups = [readings.get(name, []) for name in preferred]
    groups.extend(values for name, values in readings.items() if name not in preferred)
    for group in groups:
        for reading in group:
            if reading.current is not None:
                return float(reading.current)
    return None


def gib(value: int) -> str:
    return f"{value / 1024 ** 3:.1f} GiB"


def collect() -> dict:
    memory = psutil.virtual_memory()
    storage = psutil.disk_usage(Path.home().anchor or "/")
    return {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "temperature_c": temperature(),
            "cpu_percent": psutil.cpu_percent(interval=0.2), "cpu_count": psutil.cpu_count() or 1,
            "memory_used": memory.used, "memory_total": memory.total, "memory_percent": memory.percent,
            "storage_used": storage.used, "storage_total": storage.total, "storage_percent": storage.percent,
            "uptime_s": int(datetime.now().timestamp() - psutil.boot_time())}


async def status(arguments, context) -> ToolResult:
    data = await asyncio.to_thread(collect)
    hours, minutes = divmod(data["uptime_s"] // 60, 60)
    lines = [f"Time: {data['time']}",
             f"CPU: {data['cpu_percent']:.1f}% of {data['cpu_count']} cores"
             + (f", {data['temperature_c']:.1f} °C" if data["temperature_c"] is not None else ""),
             f"Memory: {gib(data['memory_used'])} / {gib(data['memory_total'])} ({data['memory_percent']:.0f}%)",
             f"Storage: {gib(data['storage_used'])} / {gib(data['storage_total'])} ({data['storage_percent']:.0f}%)",
             f"Uptime: {hours // 24}d {hours % 24}h {minutes}m"]
    return ToolResult.ok("\n".join(lines), data=data)


def setup(context) -> Contributions:
    return Contributions(capabilities=[Capability(
        "system.status", "Current host status: CPU, temperature, memory, storage and uptime.", status,
        risk=Risk.READ, command="status")])


EXTENSION = Extension("system", setup, contracts="1", version="1", description="Host status")
