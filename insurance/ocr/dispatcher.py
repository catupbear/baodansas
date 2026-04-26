"""多引擎调度器 — 优先文本层，降级并行云 OCR"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .base import BaseOCREngine, OCRResult
from .pdfplumber_engine import PdfplumberEngine

logger = logging.getLogger(__name__)


class OCRDispatcher:
    """
    OCR 多引擎调度器

    调度策略:
    1. 先跑 pdfplumber 提取文本层
    2. 有文本层 → 直接返回 [text_layer_result]
    3. 无文本层 → ThreadPoolExecutor 并行调所有 cloud_engines
    """

    def __init__(self, engines: Optional[List[BaseOCREngine]] = None):
        """
        初始化调度器

        Args:
            engines: 云 OCR 引擎列表（如百度、腾讯），pdfplumber 内置无需传入
        """
        self._pdfplumber = PdfplumberEngine()
        self._cloud_engines: List[BaseOCREngine] = engines or []

    def dispatch(self, pdf_bytes: bytes, page: int = 1) -> List[OCRResult]:
        """
        调度 OCR 引擎提取文本

        Args:
            pdf_bytes: PDF 原始字节数据
            page: 要提取的页码（从1开始）

        Returns:
            OCRResult 列表（至少包含 pdfplumber 结果）
        """
        # 1. 先跑 pdfplumber
        text_result = self._pdfplumber.extract(pdf_bytes, page)
        logger.info(
            "pdfplumber 提取完成: success=%s, has_text_layer=%s, cost=%dms",
            text_result.success, text_result.has_text_layer, text_result.cost_ms,
        )

        # 2. 有文本层 → 直接返回
        if text_result.success and text_result.has_text_layer:
            return [text_result]

        # 3. 无文本层 → 并行调所有云引擎
        if not self._cloud_engines:
            logger.warning("无文本层且无可用云 OCR 引擎")
            return [text_result]

        results: List[OCRResult] = []

        with ThreadPoolExecutor(max_workers=len(self._cloud_engines)) as executor:
            future_map = {
                executor.submit(engine.extract, pdf_bytes, page): engine.engine_name
                for engine in self._cloud_engines
            }
            for future in as_completed(future_map):
                engine_name = future_map[future]
                try:
                    result = future.result()
                    logger.info(
                        "云引擎 %s 完成: success=%s, confidence=%.2f, cost=%dms",
                        engine_name, result.success, result.confidence, result.cost_ms,
                    )
                    if result.success:
                        results.append(result)
                except Exception as e:
                    logger.error("云引擎 %s 异常: %s", engine_name, e)
                    results.append(OCRResult(
                        engine=engine_name,
                        success=False,
                        error=str(e),
                    ))

        # 如果所有云引擎都失败，仍返回 pdfplumber 的失败结果
        if not results:
            return [text_result]

        return results
