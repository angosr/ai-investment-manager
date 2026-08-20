"""主机资源采样：只给整体 CPU / 内存 / 磁盘使用率与负载，不做分核。

采样点是观测台进程所在主机；多进程部署下代表观测台宿主机，不冒充按角色隔离。
"""

from __future__ import annotations

import psutil


def sample_host_resources(*, disk_path: str = "/") -> dict:
    """采样一次主机资源。``cpu_percent(interval=None)`` 返回上次调用以来的占用，

    因此进程内需要有一个先前的采样点；``prime_cpu_sampler`` 在服务启动时预热。
    """

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(disk_path)
    load1, load5, load15 = _load_average()
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "memory": {
            "used_bytes": memory.used,
            "total_bytes": memory.total,
            "percent": round(memory.percent, 1),
        },
        "disk": {
            "used_bytes": disk.used,
            "total_bytes": disk.total,
            "percent": round(disk.percent, 1),
        },
        "load_average": {"1m": load1, "5m": load5, "15m": load15},
    }


def prime_cpu_sampler() -> None:
    """预热 CPU 采样器，让首次 ``sample_host_resources`` 不返回 0.0。"""

    psutil.cpu_percent(interval=None)


def _load_average() -> tuple[float | None, float | None, float | None]:
    getloadavg = getattr(psutil, "getloadavg", None)
    if getloadavg is None:  # 平台不支持（如部分 Windows）时保持失败关闭：返回未知
        return (None, None, None)
    try:
        one, five, fifteen = getloadavg()
    except (OSError, NotImplementedError):
        return (None, None, None)
    return (round(one, 2), round(five, 2), round(fifteen, 2))
