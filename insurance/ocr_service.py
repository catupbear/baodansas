#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR文本提取服务 - 基于 pdfplumber 从PDF提取文本层
支持 base64 和 bytes 两种输入方式
"""

import base64
import io
import re
from typing import Dict, Any
import pdfplumber


def clean_text(text: str) -> str:
    """清理OCR提取文本中的数字间空格和日期字段间空格"""
    # 连续执行3次，确保多层嵌套数字间空格被清理
    for _ in range(3):
        text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
    # 清理数字与日期单位之间的空格
    text = re.sub(r'(\d)\s+([年月日时分秒])', r'\1\2', text)
    # 清理日期单位与后续数字之间的空格
    text = re.sub(r'([年月日])\s+(\d)', r'\1\2', text)
    return text


def _extract_page_text(page, y_gap: float = 4.0) -> str:
    """从 pdfplumber 页面提取文本，使用字符级聚类解决标签-值分层问题

    某些PDF（如华安保险）表格标签和值在不同图层，y坐标微差（<2px），
    默认 extract_text() 会将它们拆分到不同行。
    先尝试默认提取，若检测到分层现象则用字符聚类重新组装。
    """
    raw_text = page.extract_text()
    if not raw_text:
        return ""

    # 检测是否存在标签-值分层：标签行只有字段名没有值
    # 华安特征："投保 名 称\n人 联系电话"（标签和值分离）
    if not re.search(r'投保.*名\s*称.*[一-鿿]{2,}(?:公司|[一-鿿]{2})', raw_text[:500]):
        chars = page.chars
        if chars and any(c['text'] == '投' for c in chars[:200]):
            # 尝试字符级聚类
            clustered = _cluster_chars_to_text(chars, y_gap)
            if clustered and '投保' in clustered[:500]:
                # 验证聚类结果确实合并了标签和值
                if re.search(r'投保.*名.*称.*[一-鿿]{2,}', clustered[:500]):
                    return clustered
    return raw_text


def _cluster_chars_to_text(chars: list, y_gap: float = 4.0) -> str:
    """将字符按 y 坐标聚类分行，同行按 x 排序拼接"""
    if not chars:
        return ""
    chars_sorted = sorted(chars, key=lambda c: (c['top'], c['x0']))
    lines = []
    current_line = [chars_sorted[0]]
    for c in chars_sorted[1:]:
        if abs(c['top'] - current_line[0]['top']) < y_gap:
            current_line.append(c)
        else:
            lines.append(current_line)
            current_line = [c]
    lines.append(current_line)

    result = []
    for line_chars in lines:
        line_chars.sort(key=lambda x: x['x0'])
        # 在 x 间距大的字符间插入空格
        parts = []
        for j, c in enumerate(line_chars):
            if j > 0 and c['x0'] - line_chars[j-1]['x0'] > line_chars[j-1].get('width', 8) * 1.8:
                parts.append(' ')
            parts.append(c['text'])
        result.append(''.join(parts))
    return '\n'.join(result)


def _extract_from_file_obj(pdf_file: io.BytesIO, pdf_page: int = 0) -> Dict[str, Any]:
    """
    内部方法：从 BytesIO 对象中提取文本

    Args:
        pdf_file: 已封装为 BytesIO 的 PDF 数据
        pdf_page: 要提取的页码（从1开始，0表示提取所有页）

    Returns:
        包含提取结果的字典
    """
    with pdfplumber.open(pdf_file) as pdf:
        if not pdf.pages:
            raise Exception("PDF文件没有任何页面")

        if pdf_page > 0:
            page_idx = min(pdf_page - 1, len(pdf.pages) - 1)
            pages_to_extract = [pdf.pages[page_idx]]
        else:
            pages_to_extract = pdf.pages

        all_texts = []
        for i, page in enumerate(pages_to_extract):
            raw_text = _extract_page_text(page)
            if raw_text:
                page_num = (pdf_page if pdf_page > 0 else i + 1)
                all_texts.append(f"[PAGE:{page_num}]\n{raw_text}")

        # 无文本层（扫描件）时返回失败结果
        if not all_texts:
            return {
                "success": False,
                "error": "无文本层（扫描件），暂不支持识别",
                "text": "",
                "page_count": len(pdf.pages),
            }

        combined = "\n".join(all_texts)
        text = clean_text(combined)
        return {
            "success": True,
            "text": text,
            "lines": text.split("\n"),
            "page_count": len(pdf.pages),
            "char_count": len(text),
        }


def extract_text_from_pdf(file_data_base64: str, pdf_page: int = 0) -> Dict[str, Any]:
    """
    从 base64 编码的 PDF 中提取文本

    Args:
        file_data_base64: Base64 编码的 PDF 数据
        pdf_page: 要提取的页码（从1开始，0=提取所有页）

    Returns:
        包含提取结果的字典
    """
    pdf_bytes = base64.b64decode(file_data_base64)
    pdf_file = io.BytesIO(pdf_bytes)
    return _extract_from_file_obj(pdf_file, pdf_page)


def extract_text_from_pdf_bytes(pdf_bytes: bytes, pdf_page: int = 0) -> Dict[str, Any]:
    """
    直接从 bytes 数据中提取 PDF 文本（供内部调用，无需经过 base64 编解码）

    Args:
        pdf_bytes: 原始 PDF 字节数据
        pdf_page: 要提取的页码（从1开始，0=提取所有页）

    Returns:
        包含提取结果的字典
    """
    pdf_file = io.BytesIO(pdf_bytes)
    return _extract_from_file_obj(pdf_file, pdf_page)
