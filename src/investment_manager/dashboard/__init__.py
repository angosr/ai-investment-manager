"""只读运行观测台：把既有业务事实投影为 Web 可视化，不写库、不控制。

设计文档见 ``docs/DASHBOARD_DESIGN.md``。本包只做三件事：读既有事实、投影成
plain DTO、经 Starlette 暴露只读 JSON 与 SSE。指标口径以既有事实为准，前端不重算。
"""

from __future__ import annotations

from investment_manager.dashboard.app import create_app

__all__ = ["create_app"]
