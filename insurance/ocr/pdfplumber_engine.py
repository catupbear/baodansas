"""pdfplumber OCR 引擎 — 提取 PDF 文本层"""

import io
import re
import time

import pdfplumber

from .base import BaseOCREngine, OCRResult


def clean_text(text: str) -> str:
    """清理 OCR 提取文本中的数字间空格和日期字段间空格"""
    # 连续执行3次，确保多层嵌套数字间空格被清理
    for _ in range(3):
        text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
    # 清理数字与日期单位之间的空格
    text = re.sub(r'(\d)\s+([年月日时分秒])', r'\1\2', text)
    # 清理日期单位与后续数字之间的空格
    text = re.sub(r'([年月日])\s+(\d)', r'\1\2', text)
    return text


class PdfplumberEngine(BaseOCREngine):
    """基于 pdfplumber 的文本层提取引擎"""

    engine_name = "pdfplumber"
    engine_type = "text_layer"

    def extract(self, pdf_bytes: bytes, page: int = 1) -> OCRResult:
        """
        从 PDF 文本层提取文字

        Args:
            pdf_bytes: PDF 原始字节数据
            page: 要提取的页码（从1开始）

        Returns:
            OCRResult，文本层成功时 confidence=1.0, has_text_layer=True
        """
        start = time.time()
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            with pdfplumber.open(pdf_file) as pdf:
                if not pdf.pages:
                    return OCRResult(
                        engine=self.engine_name,
                        success=False,
                        error="PDF 文件没有任何页面",
                        cost_ms=int((time.time() - start) * 1000),
                    )

                page_count = len(pdf.pages)
                # 页码越界时取最后一页
                page_idx = min(page - 1, page_count - 1)
                raw_text = pdf.pages[page_idx].extract_text()

                # 无文本层（扫描件）
                if not raw_text:
                    return OCRResult(
                        engine=self.engine_name,
                        success=False,
                        error="无文本层（扫描件）",
                        page_count=page_count,
                        has_text_layer=False,
                        cost_ms=int((time.time() - start) * 1000),
                    )

                text = clean_text(raw_text)
                return OCRResult(
                    engine=self.engine_name,
                    success=True,
                    text=text,
                    confidence=1.0,
                    page_count=page_count,
                    has_text_layer=True,
                    cost_ms=int((time.time() - start) * 1000),
                )
        except Exception as e:
            return OCRResult(
                engine=self.engine_name,
                success=False,
                error=f"pdfplumber 提取失败: {e}",
                cost_ms=int((time.time() - start) * 1000),
            )
