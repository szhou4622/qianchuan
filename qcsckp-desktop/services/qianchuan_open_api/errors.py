"""官方 API 的结构化错误；任何错误文本都不得包含密钥或令牌。"""

from __future__ import annotations

from typing import Any, Optional


class OfficialApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: Any = "",
        request_id: Any = "",
        endpoint: Any = "",
        http_status: Optional[int] = None,
        request_uid: Any = "",
        retry_after: Any = 0,
    ) -> None:
        super().__init__(str(message or "千川官方 API 请求失败"))
        self.code = str(code or "")
        self.request_id = str(request_id or "")
        self.endpoint = str(endpoint or "")
        self.http_status = http_status
        self.request_uid = str(request_uid or "")
        try:
            self.retry_after = max(0.0, float(retry_after or 0))
        except (TypeError, ValueError):
            self.retry_after = 0.0

    def asdict(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "message": str(self),
            "code": self.code,
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "request_uid": self.request_uid,
            "retry_after": self.retry_after,
        }


class OfficialApiNotConfigured(OfficialApiError):
    pass


class OfficialApiWriteDisabled(OfficialApiError):
    pass


class ApiRequestError(OfficialApiError):
    pass


class ApiPermissionError(ApiRequestError):
    pass


class ApiTokenError(ApiRequestError):
    pass


class ApiRateLimitError(ApiRequestError):
    pass


class ApiWriteOutcomeUnknown(ApiRequestError):
    """POST 已发送但未得到确定响应，调用方只能查询对账，禁止直接重试。"""
