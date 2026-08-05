"""千川工具生产版 V1A。

V1A 是与 v0.1.46 并行的只读生产架构。该包不导入旧版追投或停投执行器，
任何千川真实写请求都必须被 :mod:`production_v1a.security` 拒绝。
"""

from .constants import PRODUCT_VERSION, SCHEMA_VERSION

__all__ = ["PRODUCT_VERSION", "SCHEMA_VERSION"]
