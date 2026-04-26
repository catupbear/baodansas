"""OCR 引擎基类"""

import time
from dataclasses import dataclass


@dataclass
class OCRResult:
    """OCR 识别结果"""
    engine: str = ""            # pdfplumber / baidu / tencent
    success: bool = False
    text: str = ""
    confidence: float = 0.0     # 0-1
    cost_ms: int = 0
    error: str = ""
    page_count: int = 1
    has_text_layer: bool = False


class BaseOCREngine:
    """OCR 引擎基类，所有引擎需继承并实现 extract 方法"""

    engine_name: str = ""
    engine_type: str = ""      # text_layer / image

    def extract(self, pdf_bytes: bytes, page: int = 1) -> OCRResult:
        """
        从 PDF 中提取文本

        Args:
            pdf_bytes: PDF 原始字节数据
            page: 要提取的页码（从1开始）

        Returns:
            OCRResult 识别结果
        """
        raise NotImplementedError
