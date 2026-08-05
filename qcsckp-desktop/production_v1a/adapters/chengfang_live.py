from __future__ import annotations

from .base import PlatformAdapter


class ChengfangLiveAdapter(PlatformAdapter):
    adapter_name = "ChengfangLiveAdapter"
    adapter_version = "chengfang-live-v1a-2026.08.03"
    plan_system = "chengfang"
    promotion_scene = "live"
    mar_goal = 2
    plan_dataset = "overall_roi_promotion_list_for_live_v2"
    material_dataset = "overall_roi_promotion_matrial_tab_live"
    evidence_level = "A"
    read_capability_state = "read_verified"

    def plan_request_body(self, aavid: str, page: int, page_size: int):
        body = super().plan_request_body(aavid, page, page_size)
        body.update({"AdlabScene": 1, "SmartBidType": 0, "IsOverallRoi": 1})
        return body

    def control_dataset(self, assist_task_scene: int) -> str:
        return {
            1: "overall_live_roi2_onekeyspeed",
            2: "overall_live_combine_heat",
            3: "overall_live_roi2_grab_first_screen_list",
        }[assist_task_scene]
