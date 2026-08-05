from __future__ import annotations

from .base import PlatformAdapter


class ChengfangProductAdapter(PlatformAdapter):
    adapter_name = "ChengfangProductAdapter"
    adapter_version = "chengfang-product-v1a-2026.08.03"
    plan_system = "chengfang"
    promotion_scene = "product"
    mar_goal = 1
    plan_dataset = "overall_roi_promotion_list_for_product_v2"
    material_dataset = "overall_roi_promotion_matrial_tab_video_product"
    evidence_level = "A"
    read_capability_state = "read_verified"

    def plan_request_body(self, aavid: str, page: int, page_size: int):
        body = super().plan_request_body(aavid, page, page_size)
        body.update({"AdlabScene": 1, "SmartBidType": 0, "IsOverallRoi": 1})
        return body

    def material_request_body(self, aavid: str, ad_id: str, page: int, page_size: int):
        body = super().material_request_body(aavid, ad_id, page, page_size)
        body.update(
            {
                "query_type": "all",
                "roi2_material_type_v3": 1001,
                "marketing_goal": 1,
                "roi2_material_status": 1,
                "ad_id": ad_id,
                "roi2_material_video_type": 11,
            }
        )
        return body

    def control_dataset(self, assist_task_scene: int) -> str:
        return {
            1: "overall_product_roi2_task",
            2: "product_roi2_task_uni_prom",
            3: "overall_product_roi2_grab_first_screen_list",
        }[assist_task_scene]
