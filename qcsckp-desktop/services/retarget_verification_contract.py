"""Durable creation evidence; absent duration is not an empty numeric value."""
import json
from decimal import Decimal, InvalidOperation

CREATE = '/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/'
DURATION_ERROR = '新调控任务的duration与请求不一致'


def mapping(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def matches_request(body, row, data):
    try:
        return (body.get('scene') == 'MATERIAL_ADD_BUDGET'
                and str(body.get('advertiser_id')) == str(row.get('aavid'))
                and str(body.get('ad_id')) == str(row.get('ad_id'))
                and str(body.get('name') or '') == str(data.get('task_name') or '') != ''
                and sorted(map(str, body.get('material_ids') or [])) == sorted(map(str, data.get('material_ids') or []))
                and bool(data.get('material_ids'))
                and Decimal(str(body.get('budget'))) == Decimal(str(data.get('budget'))))
    except (InvalidOperation, ValueError, TypeError):
        return False


def creation_evidence(store, row, data):
    body = mapping(data.get('creation_request'))
    if data.get('submission_phase') == 'accepted' and matches_request(body, row, data):
        return body
    audits = store.execute(
        "SELECT request_summary_json FROM qianchuan_api_audit WHERE account_username=? "
        "AND request_id=? AND aavid=? AND ad_id=? AND endpoint=? AND method='POST' "
        "AND status='success' ORDER BY id DESC LIMIT 2",
        (row.get('account_username'), row.get('request_id'), row.get('aavid'), row.get('ad_id'), CREATE),
        fetch=True) or []
    if len(audits) != 1:
        return {}
    body = mapping(mapping(audits[0].get('request_summary_json')).get('body'))
    return body if matches_request(body, row, data) else {}


def is_durationless_control(body, data):
    return (data.get('promotion_scene') == 'live' and bool(body)
            and body.get('smart_bid_type') == 'SMART_BID_CUSTOM'
            and body.get('external_action') == 'AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY'
            and 'duration' not in body
            and (body.get('deep_external_action') == 'AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI'
                 or body.get('bid') not in (None, '')))


def verification_duration(store, row, data):
    value = data.get('duration')
    if value == '':
        body = creation_evidence(store, row, data)
        if not is_durationless_control(body, data):
            raise RuntimeError('历史空时长缺少直播控成本创建证据，需人工核对')
        return None
    return value
