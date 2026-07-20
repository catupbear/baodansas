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
import time
import requests
from typing import Dict, Any
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# 条款页检测（复用百度OCR同逻辑）
_CLAUSE_KEYWORDS = ['总则', '第一条', '第二条', '责任免除', '保险责任', '释义',
                     '理赔申请', '争议处理', '保险金申请', '条款', '注册号']


def _reorder_reversed_lines(line_texts: list, line_rects: list) -> list:
    """检测并修正倒序的 OCR 行序。

    部分保司 PDF（如平安东莞）文字是从页脚往页头绘制的，火山引擎 OCRNormal
    的 line_texts 按 PDF 绘制顺序返回，导致整页文本倒序（页脚在前、标题在最后），
    进而使文档类型判定（只看前500字）误判为"未知"、字段抽取错乱。

    仅当相邻行 y 坐标"递减"占多数（即整体自下而上）时才重排，正常文档不受影响。
    重排方式：按 y 聚类分行（同一视觉行 y 差小于行高的一半），行间按 y 升序、
    行内按 x 升序。
    """
    if not line_rects or len(line_rects) != len(line_texts) or len(line_texts) < 5:
        return line_texts

    ys = [r.get("y", 0) for r in line_rects]
    heights = sorted(r.get("height", 0) for r in line_rects)
    # 同一视觉行的 y 差阈值：取行高中位数的一半（归一化坐标）
    y_tol = max(heights[len(heights) // 2] * 0.5, 1e-4)

    desc = sum(1 for i in range(1, len(ys)) if ys[i] < ys[i - 1] - y_tol)
    asc = sum(1 for i in range(1, len(ys)) if ys[i] > ys[i - 1] + y_tol)
    if desc <= asc:
        return line_texts

    logger.info("检测到火山OCR行序倒序（desc=%d asc=%d），按坐标重排", desc, asc)
    # 按 y 升序聚类分行，行内按 x 升序
    indexed = sorted(range(len(line_texts)),
                     key=lambda i: (ys[i], line_rects[i].get("x", 0)))
    rows = []
    for i in indexed:
        if rows and abs(ys[i] - rows[-1][0]) < y_tol:
            rows[-1][1].append(i)
        else:
            rows.append((ys[i], [i]))
    result = []
    for _, idxs in rows:
        idxs.sort(key=lambda i: line_rects[i].get("x", 0))
        result.extend(line_texts[i] for i in idxs)
    return result


def _is_clause_page(text: str) -> bool:
    """判断是否为保险条款页"""
    if not text or len(text) < 50:
        return False
    import re
    # 如果页面包含保单标题或保单号，说明是新保单开始，不是条款页
    policy_header_patterns = [
        r'交通事故责任强制保险',
        r'(?:商业保险|综合险).*?(?:保险单|保\s*单)',
        r'(?:驾乘|意外伤害).*?(?:保险单|保\s*单)',
        r'保[险]?单号[：:\s]*[A-Za-z0-9]{10,}',
    ]
    for p in policy_header_patterns:
        if re.search(p, text):
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

        # 部分PDF文字自下而上绘制，接口按绘制顺序返回导致整页倒序，需按坐标修正
        line_texts = _reorder_reversed_lines(line_texts, data.get("line_rects", []))

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
            try:
                raw = self._call_ocr(pdf_base64)
                return self._parse_result(raw)
            except Exception as e:
                # 整份多页PDF直接发送在部分大文件/复杂首页(如带装饰性大图的页面)上会被API
                # 直接拒绝(如400 Bad Request)，重试一次改成只发首页单页PDF（跟其余页面处理
                # 方式一致），体积更小更不容易撞到上传限制（2026-07-20实测：平安车主尊享保障
                # 保单首页图文混排复杂，整份发送400，改成单页发送后能正常识别）
                logger.warning("火山引擎OCR首页整份发送失败(%s)，改拆单页重试", e)
                try:
                    pdf_bytes = base64.b64decode(pdf_base64)
                    page_b64 = self._extract_page_base64(pdf_bytes, 1)
                    if page_b64:
                        raw2 = self._call_ocr(page_b64)
                        return self._parse_result(raw2)
                except Exception as e2:
                    logger.warning("首页拆单页重试仍失败: %s", e2)
                raise

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

    def recognize_pdf_multi_pages(self, pdf_bytes: bytes, max_pages: int = 6,
                                   early_stop_check=None,
                                   page_fallback=None) -> Dict[str, Any]:
        """
        多页PDF识别，逐页调用并拼接结果。
        遇到条款页自动停止。

        Args:
            pdf_bytes: PDF 原始字节
            max_pages: 最多识别页数
            early_stop_check: 可选回调 fn(combined_text) -> bool，
            page_fallback: 可选回调 fn(pdf_bytes, page_num) -> dict，单页识别失败时的兜底函数（如百度OCR）
                              返回 True 表示核心字段已提取完毕可以提前停止
        """
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                total_pages = len(pdf.pages)
        except Exception:
            total_pages = max_pages

        pages_to_scan = min(max_pages, total_pages)

        all_texts = []

        def _try_fallback(page: int) -> None:
            """单页火山OCR失败（限流耗尽/非限流HTTP错误/其他异常）时统一尝试兜底引擎，
            成功则把该页文字补进 all_texts。之前只有429限流重试耗尽这一条路径会兜底，
            其余失败路径（如非429的HTTP错误、连接异常）直接跳页，该页文字彻底丢失——
            首页恰恰是保险期间/保费等核心字段所在页，丢了就导致这些字段全空
            （2026-07-20实测：平安车主尊享保障首页OCR返回400 Bad Request，未走429重试
            分支、也没有兜底，起保/终保日期和保费全部缺失）。"""
            if not page_fallback:
                logger.warning("第%d页无兜底引擎可用，跳过", page)
                return
            try:
                fb_result = page_fallback(pdf_bytes, page)
                if fb_result and fb_result.get("success"):
                    fb_text = fb_result.get("text", "")
                    logger.info("兜底引擎第%d页识别成功，文字数=%d", page, len(fb_text))
                    if not (page > 2 and _is_clause_page(fb_text)):
                        all_texts.append(f"[PAGE:{page}]\n{fb_text}")
                else:
                    logger.warning("兜底引擎第%d页识别也失败: %s", page, (fb_result or {}).get("error", ""))
            except Exception as fb_e:
                logger.warning("兜底引擎第%d页识别异常: %s", page, fb_e)

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
                    # 条款/广告页跳过（不加入文本），但继续扫描后续页（可能有新保单）
                    if page > 2 and _is_clause_page(page_text):
                        logger.info("火山引擎OCR第%d页检测为条款页，跳过该页继续扫描", page)
                        continue
                    # 插入页码标记，供多保单拆分时追踪页码
                    all_texts.append(f"[PAGE:{page}]\n{page_text}")
                    # 扫过至少一半总页数后，才检查核心字段是否已提取完毕
                    min_pages_before_stop = max(3, total_pages // 2)
                    if page >= min_pages_before_stop and early_stop_check and len(all_texts) >= 2:
                        combined_so_far = "\n".join(all_texts)
                        if early_stop_check(combined_so_far):
                            logger.info("火山引擎OCR第%d页后核心字段已齐全，提前停止扫描（总页数%d）", page, total_pages)
                            break
                else:
                    logger.warning("火山引擎OCR第%d页识别失败: %s", page, result.get("error", ""))
                    _try_fallback(page)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    # 限流：等待后重试，最多重试 3 次
                    for retry in range(1, 4):
                        wait = 2 * retry
                        logger.info("火山引擎OCR第%d页触发限流(429)，%d秒后第%d次重试", page, wait, retry)
                        time.sleep(wait)
                        try:
                            if page == 1:
                                pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                                result = self.recognize_pdf(pdf_b64, page_num=1)
                            else:
                                page_b64 = self._extract_page_base64(pdf_bytes, page)
                                if not page_b64:
                                    break
                                raw = self._call_ocr(page_b64)
                                result = self._parse_result(raw)
                            if result.get("success"):
                                page_text = result.get("text", "")
                                logger.info("火山引擎OCR第%d页重试成功，文字数=%d", page, len(page_text))
                                if page > 2 and _is_clause_page(page_text):
                                    logger.info("火山引擎OCR第%d页检测为条款页，跳过", page)
                                    break
                                all_texts.append(f"[PAGE:{page}]\n{page_text}")
                                break
                        except Exception as retry_e:
                            logger.warning("火山引擎OCR第%d页第%d次重试异常: %s", page, retry, retry_e)
                    else:
                        # 重试耗尽，尝试用兜底引擎识别该页
                        logger.info("火山引擎OCR第%d页限流重试3次仍失败，降级兜底引擎", page)
                        _try_fallback(page)
                    continue
                # 非429的HTTP错误（如400 Bad Request）：同样兜底，不能直接丢页
                logger.warning("火山引擎OCR第%d页异常: %s", page, e)
                _try_fallback(page)
                continue
            except Exception as e:
                logger.warning("火山引擎OCR第%d页异常: %s", page, e)
                # 兜底后仍跳过失败页继续扫描后续页（多保单PDF不能因单页失败丢失整个保单）
                _try_fallback(page)
                continue

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
