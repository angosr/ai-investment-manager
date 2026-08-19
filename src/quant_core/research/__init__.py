"""离线研究边界；生产服务不得从这里导入回测引擎。"""

from quant_core.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    InstrumentSpec,
    fetch_binance_history,
)

__all__ = [
    "HistoricalDataset",
    "HistoricalDatasetCatalog",
    "HistoricalDatasetManifest",
    "InstrumentSpec",
    "fetch_binance_history",
]
