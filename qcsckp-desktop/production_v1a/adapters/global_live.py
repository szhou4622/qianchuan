from __future__ import annotations

from .base import PlatformAdapter


class GlobalLiveAdapter(PlatformAdapter):
    adapter_name = "GlobalLiveAdapter"
    adapter_version = "global-live-v1a-2026.08.03"
    plan_system = "global"
    promotion_scene = "live"
    mar_goal = 2
    plan_dataset = "site_promotion_list"
    material_dataset = "site_promotion_post_data_live"
    evidence_level = "C"
    read_capability_state = "unobserved"

    def control_dataset(self, assist_task_scene: int) -> str:
        # 全域调控任务数据集需要真实样本回归；保留场景但不猜测键名。
        return ""
