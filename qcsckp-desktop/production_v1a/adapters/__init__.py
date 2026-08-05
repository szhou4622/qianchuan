"""四类显式、版本化千川只读适配器。"""

from .chengfang_live import ChengfangLiveAdapter
from .chengfang_product import ChengfangProductAdapter
from .global_live import GlobalLiveAdapter
from .global_product import GlobalProductAdapter
from .registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "GlobalProductAdapter",
    "GlobalLiveAdapter",
    "ChengfangProductAdapter",
    "ChengfangLiveAdapter",
]
