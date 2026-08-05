from __future__ import annotations

from typing import Iterable

from ..security import PlatformNetworkGuard
from .base import PlatformAdapter, ReadTransport
from .chengfang_live import ChengfangLiveAdapter
from .chengfang_product import ChengfangProductAdapter
from .global_live import GlobalLiveAdapter
from .global_product import GlobalProductAdapter


class AdapterRegistry:
    ADAPTER_TYPES = (
        GlobalProductAdapter,
        GlobalLiveAdapter,
        ChengfangProductAdapter,
        ChengfangLiveAdapter,
    )

    def __init__(self, transport: ReadTransport, guard: PlatformNetworkGuard | None = None):
        self.adapters = tuple(
            adapter_type(transport, guard=guard) for adapter_type in self.ADAPTER_TYPES
        )

    def all(self) -> tuple[PlatformAdapter, ...]:
        return self.adapters

    def get(self, plan_system: str, promotion_scene: str) -> PlatformAdapter:
        for adapter in self.adapters:
            if adapter.identity == (plan_system, promotion_scene):
                return adapter
        raise KeyError(f"unknown adapter identity: {plan_system}/{promotion_scene}")

    def capability_matrix(self) -> list[dict]:
        rows = []
        for adapter in self.adapters:
            writes = adapter.get_capabilities()
            rows.append({
                "adapter_key": adapter.adapter_name,
                "adapter_version": adapter.adapter_version,
                "plan_system": adapter.plan_system,
                "promotion_scene": adapter.promotion_scene,
                "evidence_level": adapter.evidence_level,
                "read_state": adapter.read_capability_state,
                "read_catalog": adapter.read_capability_state,
                "read_video_material": adapter.read_capability_state,
                "read_control_tasks": adapter.read_capability_state,
                "create_retarget": writes["can_create_volume_retarget"]["state"],
                "pause_task": writes["can_pause_control_task"]["state"],
                "adjust_task": writes["can_increase_total_budget"]["state"],
                "write_capabilities": writes,
            })
        return rows
