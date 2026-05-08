#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火山引擎OCR服务 - 通用文字识别
API文档: https://www.volcengine.com/docs/86081/1660261
使用 HMAC-SHA256 签名（V4），不依赖重量级 SDK
"""

import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
import requests
from typing import Dict, Any
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# 条款页检测（复用百度OCR同逻辑）
_CLAUSE_KEYWORDS = ['总则', '第一条', '第二条', '责任免除', '保险责任', '释义',
                     '理赔申请', '争议处理', '保险金申请', '条款', '注册号']


def _is_clause_page(text: str) -> bool:
    """判断是否为保险条款页"""
    if not text or len(text) < 50:
        return False
    return sum(1 for kw in _CLAUSE_KEYWORDS if kw in text) >= 3


class VolcOCR:
    """火山引擎OCR客户端"""

    HOST = "visual.volcengineapi.com"
    ENDPOINT = f"https://{HOST}"
    REGION = "cn-north-1"
    SERVICE = "cv"
    ACTION = "OCRNormal"
    VERSION = "2020-08-26"

    def __init__(self, access_key_id: str, secret_access_key: str):
        self.ak = access_key_id
        self.sk = secret_access_key

    # ------------------------------------------------------------------ #
    # V4 签名
    # ------------------------------------------------------------------ #

    @staticmethod
    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _get_signing_key(self, short_date: str) -> bytes:
        k_date = self._hmac_sha256(self.sk.encode("utf-8"), short_date)
        k_region = self._hmac_sha256(k_date, self.REGION)
        k_service = self._hmac_sha256(k_region, self.SERVICE)
        return self._hmac_sha256(k_service, "request")

    def _sign_request(self, body: str, query: dict) -> dict:
        """
        构造带签名的请求头。
        body: URL 编码后的请求体字符串
        query: URL 查询参数
        返回: 完整的请求头字典
        """
        now = datetime.datetime.utcnow()
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        short_date = now.strftime("%Y%m%d")

        # 请求头
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": self.HOST,
            "X-Date": x_date,
        }

        # Body hash
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        headers["X-Content-Sha256"] = body_hash

        # Signed headers（按 key 字母序排列）
        signed_headers = {}
        for k in headers:
            if k in ("Content-Type", "Host") or k.startswith("X-"):
                signed_headers[k.lower()] = headers[k]
        signed_headers_str = ""
        for k in sorted(signed_headers.keys()):
            signed_headers_str += f"{k}:{signed_headers[k]}\n"
        signed_headers_names = ";".join(sorted(signed_headers.keys()))

        # Canonical query string
        canonical_qs = "&".join(
            f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}"
            for k, v in sorted(query.items())
        )

        # Canonical request
        canonical_request = "\n".join([
            "POST", "/", canonical_qs,
            signed_headers_str, signed_headers_names, body_hash
        ])

        # String to sign
        credential_scope = f"{short_date}/{self.REGION}/{self.SERVICE}/request"
        string_to_sign = "\n".join([
            "HMAC-SHA256", x_date, credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        ])

        # Signature
        signing_key = self._get_signing_key(short_date)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"HMAC-SHA256 Credential={self.ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers_names}, Signature={signature}"
        )
        return headers

    # ------------------------------------------------------------------ #
    # 核心调用
    # ------------------------------------------------------------------ #

    def _call_ocr(self, image_base64: str) -> Dict[str, Any]:
        """
        调用火山引擎通用文字识别接口。
        image_base64: 图片或PDF的base64编码
        返回: 原始响应 JSON
        """
        query = {"Action": self.ACTION, "Version": self.VERSION}
        body_params = {"image_base64": image_base64}
        body = urlencode(body_params)

        headers = self._sign_request(body, query)

        qs = urlencode(query)
        url = f"{self.ENDPOINT}?{qs}"

        resp = requests.post(url, data=body, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _parse_result(self, result: dict) -> Dict[str, Any]:
        """解析火山引擎OCR响应"""
        code = result.get("code", -1)
        if code != 10000:
            msg = result.get("message", "未知错误")
            return {
                "success": False,
                "error": f"火山引擎OCR识别失败: {msg} (code={code})",
                "text": "",
            }

        data = result.get("data", {})
        line_texts = data.get("line_texts", [])
        if not line_texts:
            return {
                "success": False,
                "error": "火山引擎OCR未识别到任何文字",
                "text": "",
            }

        text = "\n".join(line_texts)
        return {
            "success": True,
            "text": text,
            "lines": line_texts,
            "words_count": len(line_texts),
            "char_count": len(text),
            "ocr_engine": "volc",
        }

    # ------------------------------------------------------------------ #
    # 公开接口（与百度OCR接口保持一致）
    # ------------------------------------------------------------------ #

    def recognize_pdf(self, pdf_base64: str, page_num: int = 1) -> Dict[str, Any]:
        """
        识别PDF中的文字。
        火山引擎 OCRNormal 默认只识别第一页，多页需拆页调用。
        page_num: 页码（从1开始），非第一页时内部拆页。
        """
        if page_num == 1:
            raw = self._call_ocr(pdf_base64)
            return self._parse_result(raw)

        # 非第一页：拆出指定页重新编码
        try:
            pdf_bytes = base64.b64decode(pdf_base64)
            page_b64 = self._extract_page_base64(pdf_bytes, page_num)
            if not page_b64:
                return {"success": False, "error": f"无法提取第{page_num}页", "text": ""}
            raw = self._call_ocr(page_b64)
            return self._parse_result(raw)
        except Exception as e:
            return {"success": False, "error": f"拆页失败: {e}", "text": ""}

    def recognize_pdf_bytes(self, pdf_bytes: bytes, page_num: int = 1) -> Dict[str, Any]:
        """识别PDF（bytes输入）"""
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
        return self.recognize_pdf(pdf_base64, page_num)

    def recognize_pdf_multi_pages(self, pdf_bytes: bytes, max_pages: int = 6) -> Dict[str, Any]:
        """
        多页PDF识别，逐页调用并拼接结果。
        遇到条款页自动停止。
        """
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                total_pages = len(pdf.pages)
        except Exception:
            total_pages = max_pages

        pages_to_scan = min(max_pages, total_pages)

        all_texts = []
        for page in range(1, pages_to_scan + 1):
            try:
                if page == 1:
                    # 第一页直接用原始PDF
                    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                    result = self.recognize_pdf(pdf_b64, page_num=1)
                else:
                    # 后续页拆页
                    page_b64 = self._extract_page_base64(pdf_bytes, page)
                    if not page_b64:
                        break
                    raw = self._call_ocr(page_b64)
                    result = self._parse_result(raw)

                if result.get("success"):
                    page_text = result.get("text", "")
                    logger.info("火山引擎OCR第%d/%d页识别成功，文字数=%d", page, pages_to_scan, len(page_text))
                    # 至少扫描前2页（签单日期等字段常在第2页），第3页起检测条款页
                    if page > 2 and _is_clause_page(page_text):
                        logger.info("火山引擎OCR第%d页检测为条款页，停止扫描", page)
                        break
                    # 插入页码标记，供多保单拆分时追踪页码
                    all_texts.append(f"[PAGE:{page}]\n{page_text}")
                else:
                    logger.warning("火山引擎OCR第%d页识别失败: %s", page, result.get("error", ""))
            except Exception as e:
                logger.warning("火山引擎OCR第%d页异常: %s", page, e)
                break

        if not all_texts:
            return {"success": False, "error": "火山引擎OCR多页识别均失败", "text": ""}

        combined = "\n".join(all_texts)
        return {
            "success": True,
            "text": combined,
            "lines": combined.split("\n"),
            "words_count": combined.count("\n") + 1,
            "char_count": len(combined),
            "ocr_engine": "volc",
            "pages_scanned": len(all_texts),
        }

    def recognize_image(self, image_base64: str) -> Dict[str, Any]:
        """识别图片中的文字（base64输入）"""
        raw = self._call_ocr(image_base64)
        return self._parse_result(raw)

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_page_base64(pdf_bytes: bytes, page_num: int) -> str:
        """从PDF中提取指定页，返回单页PDF的base64编码"""
        try:
            from PyPDF2 import PdfReader, PdfWriter
        except ImportError:
            try:
                from pypdf import PdfReader, PdfWriter
            except ImportError:
                logger.error("需要 PyPDF2 或 pypdf 库来拆页，请安装: pip install pypdf")
                return ""

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            if page_num > len(reader.pages):
                return ""
            writer = PdfWriter()
            writer.add_page(reader.pages[page_num - 1])
            buf = io.BytesIO()
            writer.write(buf)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.warning("PDF拆页失败(page=%d): %s", page_num, e)
            return ""
