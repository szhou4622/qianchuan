"""商品全域页面的只读响应适配器。

千川商品全域当前停留在 ``/uni-prom``，主计划、商品和素材关系需要从接口响应中
确认。这里仅解析业务标识与名称，不保存 Cookie、Token、请求头或完整响应。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from services.plan_system import detect_plan_system


PRODUCT_PLAN_API_PATHS = frozenset(
    {
        "/ad/api/creation/v1/ad/ad-detail-plus",
        "/ad/api/creation/v1/shop-prom/get-config",
    }
)
PRODUCT_AD_LIST_API_PATHS = frozenset(
    {
        "/ad/api/pmc/v1/uni-promotion/ad/list-required",
        "/ad/api/pmc/v1/uni-promotion/ad/list-optional",
    }
)


def _text(value: Any, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


async def find_visible_exact_text(
    page: Any,
    text: str,
    *,
    timeout_ms: int = 60_000,
) -> Optional[Any]:
    """返回精确文本的可见节点，避免命中千川页面里的隐藏重复节点。"""
    locator = page.get_by_text(str(text), exact=True)
    deadline = max(1_000, int(timeout_ms))
    waited = 0
    while waited < deadline:
        count = await locator.count()
        for index in range(count - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        step = min(250, deadline - waited)
        await page.wait_for_timeout(step)
        waited += step
    return None


def _first_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _iter_mappings(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, Mapping):
        yield dict(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield dict(item)


def extract_safe_query_identifiers(url: str) -> Dict[str, str]:
    """只从 URL 查询参数提取账户/计划标识，不返回完整 URL 或其它参数。"""
    query = parse_qs(urlparse(str(url or "")).query)

    def first(*names: str) -> str:
        for name in names:
            values = query.get(name)
            if not values:
                continue
            value = _text(values[0], 64)
            if value.isdigit():
                return value
        return ""

    result = {
        "aavid": first("aavid", "aadvid", "advertiserId", "advertiser_id"),
        "ad_id": first("adId", "adid", "ad_id"),
    }
    return {key: value for key, value in result.items() if value}


def _product(product: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    product_id = _text(
        product.get("productId")
        or product.get("product_id")
        or product.get("goodsId")
        or product.get("id"),
        64,
    )
    if not product_id:
        return None
    return {
        "product_id": product_id,
        "product_name": _text(
            product.get("productName")
            or product.get("goodsName")
            or product.get("name")
            or product.get("title")
        ),
    }


def _material(material: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    material_id = _text(
        material.get("materialId")
        or material.get("material_id")
        or material.get("videoId")
        or material.get("id"),
        64,
    )
    if not material_id:
        return None
    return {
        "material_id": material_id,
        "material_name": _text(
            material.get("materialName")
            or material.get("title")
            or material.get("name")
        ),
    }


def _plan(plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    plan_id = _text(plan.get("adId") or plan.get("ad_id") or plan.get("id"), 64)
    if not plan_id:
        return None
    result: Dict[str, Any] = {
        "ad_id": plan_id,
        "plan_name": _text(
            plan.get("adName") or plan.get("planName") or plan.get("name")
        ),
        "plan_system": detect_plan_system(payload=dict(plan)),
    }
    for source, target in (
        ("adDeliveryName", "delivery_name"),
        ("adDeliveryType", "delivery_type"),
        ("creativeType", "creative_type"),
        ("budget", "budget"),
        ("ecpRoi2Goal", "roi_goal"),
        ("roiGoal", "roi_goal"),
    ):
        if source in plan and plan.get(source) not in (None, ""):
            result[target] = plan.get(source)
    return result


def extract_product_scene_snapshot(payload: Any) -> Dict[str, Any]:
    """解析商品全域主计划、商品、子广告及素材关系。"""
    root = _first_mapping(payload)
    data = _first_mapping(root.get("data"))

    plan: Optional[Dict[str, Any]] = None
    for block in (data.get("adDetailInfo"), data.get("availableAdInfo")):
        if isinstance(block, Mapping):
            plan = _plan(block)
            if plan:
                if plan.get("plan_system") == "unknown":
                    plan["plan_system"] = detect_plan_system(payload=root)
                break

    products: Dict[str, Dict[str, str]] = {}

    def add_product(value: Any) -> None:
        for item in _iter_mappings(value):
            parsed = _product(item)
            if not parsed:
                continue
            current = products.get(parsed["product_id"])
            if current and not parsed.get("product_name"):
                continue
            products[parsed["product_id"]] = parsed

    add_product(data.get("goodsInfos"))
    for creative in _iter_mappings(data.get("createMultiProductsCreative")):
        add_product(creative)

    ad_product_map: Dict[str, List[str]] = {}
    raw_ad_goods = data.get("adGoodsMap")
    if isinstance(raw_ad_goods, Mapping):
        for raw_ad_id, value in raw_ad_goods.items():
            ad_id = _text(raw_ad_id, 64)
            product_ids: List[str] = []
            for item in _iter_mappings(value):
                parsed = _product(item)
                if not parsed:
                    continue
                products[parsed["product_id"]] = parsed
                if parsed["product_id"] not in product_ids:
                    product_ids.append(parsed["product_id"])
            if ad_id and product_ids:
                ad_product_map[ad_id] = product_ids

    ad_rows: List[Dict[str, Any]] = []
    for item in _iter_mappings(data.get("adInfos")):
        parsed_plan = _plan(item)
        if not parsed_plan:
            continue
        ad_rows.append(
            {
                "ad_id": parsed_plan["ad_id"],
                "ad_name": parsed_plan.get("plan_name") or "",
                "product_ids": list(ad_product_map.get(parsed_plan["ad_id"], [])),
            }
        )

    materials: List[Dict[str, Any]] = []
    seen_materials = set()
    material_map = data.get("adShowMaterialInfoMap")
    if isinstance(material_map, Mapping):
        for raw_ad_id, raw_info in material_map.items():
            ad_id = _text(raw_ad_id, 64)
            info = _first_mapping(raw_info)
            blocks: List[Any] = []
            for key in (
                "promotionVideoMaterial",
                "videoMaterial",
                "material",
                "materials",
            ):
                if info.get(key) is not None:
                    blocks.append(info.get(key))
            for block in blocks:
                for material_info in _iter_mappings(block):
                    parsed_material = _material(material_info)
                    if not parsed_material:
                        continue
                    marker = (ad_id, parsed_material["material_id"])
                    if marker in seen_materials:
                        continue
                    seen_materials.add(marker)
                    materials.append(
                        {
                            **parsed_material,
                            "ad_id": ad_id,
                            "product_ids": list(ad_product_map.get(ad_id, [])),
                        }
                    )

    migration = _first_mapping(data.get("info")).get("migrationDataList")
    for item in _iter_mappings(migration):
        add_product(item.get("product"))

    return {
        "plan": plan,
        "products": list(products.values()),
        "ad_rows": ad_rows,
        "materials": materials,
    }


def merge_product_scene_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """合并同一页面不同接口返回的脱敏快照。"""
    plan: Optional[Dict[str, Any]] = None
    products: Dict[str, Dict[str, Any]] = {}
    ad_rows: Dict[str, Dict[str, Any]] = {}
    materials: Dict[tuple[str, str], Dict[str, Any]] = {}

    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        candidate_plan = snapshot.get("plan")
        if isinstance(candidate_plan, Mapping) and candidate_plan.get("ad_id"):
            if not plan or candidate_plan.get("plan_name"):
                plan = dict(candidate_plan)
        for item in snapshot.get("products") or []:
            if isinstance(item, Mapping) and item.get("product_id"):
                products[str(item["product_id"])] = dict(item)
        for item in snapshot.get("ad_rows") or []:
            if isinstance(item, Mapping) and item.get("ad_id"):
                ad_rows[str(item["ad_id"])] = dict(item)
        for item in snapshot.get("materials") or []:
            if not isinstance(item, Mapping) or not item.get("material_id"):
                continue
            marker = (
                str(item.get("ad_id") or ""),
                str(item.get("material_id") or ""),
            )
            materials[marker] = dict(item)

    return {
        "plan": plan,
        "products": list(products.values()),
        "ad_rows": list(ad_rows.values()),
        "materials": list(materials.values()),
    }


def scope_product_scene_snapshot(
    snapshot: Mapping[str, Any],
    *,
    ad_id: Any,
) -> Dict[str, Any]:
    """Restrict an account-wide product snapshot to one monitored plan."""
    target_ad_id = _text(ad_id, 64)
    result = {
        "plan": dict(snapshot.get("plan") or {}),
        "products": list(snapshot.get("products") or []),
        "ad_rows": list(snapshot.get("ad_rows") or []),
        "materials": list(snapshot.get("materials") or []),
    }
    if not target_ad_id:
        return result

    target_rows = [
        dict(item)
        for item in result["ad_rows"]
        if isinstance(item, Mapping)
        and _text(item.get("ad_id"), 64) == target_ad_id
    ]
    if not target_rows:
        return result

    product_ids = {
        _text(product_id, 64)
        for item in target_rows
        for product_id in (item.get("product_ids") or [])
        if _text(product_id, 64)
    }
    result["ad_rows"] = target_rows
    result["products"] = [
        dict(item)
        for item in result["products"]
        if isinstance(item, Mapping)
        and _text(item.get("product_id"), 64) in product_ids
    ]
    result["materials"] = [
        {
            **dict(item),
            "product_ids": [
                _text(product_id, 64)
                for product_id in (item.get("product_ids") or [])
                if _text(product_id, 64) in product_ids
            ],
        }
        for item in result["materials"]
        if isinstance(item, Mapping)
        and _text(item.get("ad_id"), 64) == target_ad_id
    ]
    return result


def _find_identifier(
    payload: Any,
    names: frozenset[str],
    *,
    max_nodes: int = 160,
) -> str:
    queue: List[Any] = [payload]
    nodes = 0
    while queue and nodes < max_nodes:
        value = queue.pop(0)
        nodes += 1
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key) in names:
                    text = _text(child, 64)
                    if text.isdigit():
                        return text
                if isinstance(child, (Mapping, list)):
                    queue.append(child)
        elif isinstance(value, list):
            queue.extend(value[:60])
    return ""


def validate_exact_product_plan_payload(
    payload: Any,
    *,
    expected_ad_id: Any,
    require_delivering: bool = True,
) -> Optional[str]:
    """Validate the exact plan response used immediately before a product write."""
    root = _first_mapping(payload)
    status_code = root.get("status_code")
    if status_code is not None and status_code != 0:
        return _text(root.get("message")) or "商品全域计划详情接口返回失败"
    detail = _first_mapping(_first_mapping(root.get("data")).get("adDetailInfo"))
    if not detail:
        return "商品全域计划详情未返回主计划"
    expected_plan = _text(expected_ad_id, 64)
    actual_plan = _text(
        detail.get("id") or detail.get("adId") or detail.get("ad_id"),
        64,
    )
    if actual_plan != expected_plan:
        return (
            f"商品全域计划不匹配：期望 {expected_plan}，实际 "
            f"{actual_plan or '未返回'}"
        )
    if require_delivering:
        delivery_name = _text(detail.get("adDeliveryName"), 64)
        try:
            delivery_type = int(detail.get("adDeliveryType"))
        except (TypeError, ValueError):
            delivery_type = -1
        if delivery_name != "投放中" and delivery_type != 0:
            return (
                "商品全域计划当前非投放中："
                f"{delivery_name or '未知状态'}"
            )
    return None


async def goto_and_confirm_product_target(
    page: Any,
    url: str,
    *,
    expected_aavid: Any,
    expected_ad_id: Any,
    timeout_ms: int = 60_000,
) -> Optional[str]:
    """打开商品自选列表并同时确认可见计划行和精确计划接口；成功返回 None。"""
    expected_account = _text(expected_aavid, 64)
    expected_plan = _text(expected_ad_id, 64)
    if not expected_account or not expected_plan:
        return "商品全域任务缺少账户或计划ID，已安全停止"
    try:
        # /uni-prom/detail 会被商品全域前端重写，并可能落到该账户的默认计划。
        # 从中性列表页进入，再搜索精确计划，确保后续表单属于目标行。
        list_url = (
            "https://qianchuan.jinritemai.com/uni-prom"
            f"?aavid={expected_account}"
        )
        id_node = None
        last_error: Optional[Exception] = None
        per_attempt_timeout = max(15_000, min(int(timeout_ms), 45_000))
        for attempt in range(2):
            try:
                await page.goto(
                    list_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                current_host = urlparse(
                    str(getattr(page, "url", "") or "")
                ).netloc.lower()
                if current_host != "qianchuan.jinritemai.com":
                    return "千川登录状态已失效或页面未进入千川，已安全停止"

                product_tab = page.get_by_text("商品自选", exact=True)
                await product_tab.last.wait_for(
                    state="visible",
                    timeout=timeout_ms,
                )
                product_tab_count = await product_tab.count()
                clicked_tab = False
                for index in range(product_tab_count - 1, -1, -1):
                    candidate = product_tab.nth(index)
                    if await candidate.is_visible():
                        await candidate.click()
                        clicked_tab = True
                        break
                if not clicked_tab:
                    return "未找到商品全域的「商品自选」入口，已安全停止"

                search_input = page.locator(
                    'input[placeholder="输入计划名称/ID后回车搜索"]'
                )
                await search_input.first.wait_for(
                    state="visible",
                    timeout=timeout_ms,
                )
                if attempt:
                    await search_input.first.fill("")
                    await search_input.first.press("Enter")
                    await page.wait_for_timeout(750)
                await search_input.first.fill(expected_plan)
                await search_input.first.press("Enter")
                id_node = await find_visible_exact_text(
                    page,
                    f"ID：{expected_plan}",
                    timeout_ms=per_attempt_timeout,
                )
                if id_node is not None:
                    break
                raise TimeoutError(
                    f"商品自选列表等待计划 {expected_plan} 超时"
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    continue
                raise
        if id_node is None:
            raise last_error or TimeoutError(
                f"商品自选列表未找到计划 {expected_plan}"
            )
        plan_row = id_node.locator("xpath=ancestor::tr[1]")
        if await plan_row.count() < 1:
            return f"商品自选列表未找到计划 {expected_plan}，已安全停止"
        row_text = _text(await plan_row.inner_text(), 4096)
        if expected_plan not in row_text:
            return f"商品自选列表计划不匹配：期望 {expected_plan}"
        if "投放中" not in row_text:
            return f"商品全域计划当前非投放中：{row_text[:160] or '未知状态'}"

        payload = await page.evaluate(
            """async ({ aavid, adId }) => {
                const query = new URLSearchParams({ aavid, adid: adId });
                const response = await fetch(
                    `/ad/api/creation/v1/ad/ad-detail-plus?${query.toString()}`,
                    { credentials: "include" }
                );
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return await response.json();
            }""",
            {"aavid": expected_account, "adId": expected_plan},
        )
    except Exception as exc:
        return f"商品全域精确计划定位或详情读取失败：{exc}"

    actual_account = _find_identifier(
        payload,
        frozenset({"aavid", "aadvid", "advertiserId", "advertiser_id"}),
    )
    if actual_account and actual_account != expected_account:
        return (
            f"商品全域账户不匹配：期望 {expected_account}，实际 "
            f"{actual_account}"
        )
    return validate_exact_product_plan_payload(
        payload,
        expected_ad_id=expected_plan,
        require_delivering=True,
    )
