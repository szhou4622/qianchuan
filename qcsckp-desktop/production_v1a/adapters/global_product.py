from __future__ import annotations

from .base import PlatformAdapter


class GlobalProductAdapter(PlatformAdapter):
    adapter_name = "GlobalProductAdapter"
    adapter_version = "global-product-v1a-2026.08.03"
    plan_system = "global"
    promotion_scene = "product"
    mar_goal = 1
    plan_dataset = "overall_roi_promotion_list_for_product"
    material_dataset = "site_promotion_product_post_data_video"
    evidence_level = "C"
    read_capability_state = "unobserved"

    def control_dataset(self, assist_task_scene: int) -> str:
        return ""
