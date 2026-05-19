#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度OCR服务 - 通用文字识别（高精度版）
当 pdfplumber 无法提取文本层时（扫描件），降级使用百度OCR识别
API文档: https://cloud.baidu.com/doc/OCR/s/1k3h7y3db
"""

import base64
import time
import requests
from typing import Dict, Any, Optional


def _is_clause_page(text: str) -> bool:
    """判断是否为保险条款页（非保单数据页），用于多页识别时提前终止"""
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
    # 条款页特征：含"第X条"、"总则"、"责任免除"等法律条文关键词
    clause_keywords = ['总则', '第一条', '第二条', '责任免除', '保险责任', '释义',
                        '理赔申请', '争议处理', '保险金申请', '条款', '注册号']
    hit_count = sum(1 for kw in clause_keywords if kw in text)
    # 命中3个以上特征词视为条款页
    return hit_count >= 3


class BaiduOCR:
    """百度OCR客户端，支持高精度版和标准版切换"""

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    # 高精度版（accurate）和标准版（standard）的 API 地址
    OCR_URLS = {
        "accurate": "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
        "standard": "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
    }

    def __init__(self, api_key: str, secret_key: str, accuracy: str = "accurate"):
        self.api_key = api_key
        self.secret_key = secret_key
        # 无 accuracy 参数时默认高精度版
        self.accuracy = accuracy if accuracy in self.OCR_URLS else "accurate"
        self.ocr_url = self.OCR_URLS[self.accuracy]
        self._access_token: Optional[str] = None
        self._token_expire_time: float = 0

    def _get_access_token(self) -> str:
        """获取 access_token（带缓存，有效期30天）"""
        if self._access_token and time.time() < self._token_expire_time:
            return self._access_token

        resp = requests.post(self.TOKEN_URL, params={
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        })
        resp.raise_for_status()
        result = resp.json()

        if "access_token" not in result:
            raise Exception(f"百度OCR获取token失败: {result.get('error_description', '未知错误')}")

        self._access_token = result["access_token"]
        # token有效期通常30天，提前1小时标记为过期
        self._token_expire_time = time.time() + result.get("expires_in", 2592000) - 3600
        return self._access_token

    def _parse_result(self, result: dict) -> Dict[str, Any]:
        """
        解析百度OCR接口返回的识别结果（公共逻辑，消除重复代码）

        Args:
            result: 百度OCR接口原始响应 JSON

        Returns:
            标准化的识别结果字典
        """
        # 接口返回错误码
        if "error_code" in result:
            return {
                "success": False,
                "error": f"百度OCR识别失败: {result.get('error_msg', '未知错误')} (错误码: {result['error_code']})",
                "text": "",
            }

        words_list = result.get("words_result", [])
        if not words_list:
            return {
                "success": False,
                "error": "百度OCR未识别到任何文字",
                "text": "",
            }

        text = "\n".join(item["words"] for item in words_list)
        return {
            "success": True,
            "text": text,
            "lines": [item["words"] for item in words_list],
            "words_count": result.get("words_result_num", 0),
            "char_count": len(text),
            "ocr_engine": "baidu",
        }

    def recognize_pdf(self, pdf_base64: str, page_num: int = 1) -> Dict[str, Any]:
        """
        识别 PDF 文件中的文字（base64 输入）

        Args:
            pdf_base64: Base64 编码的 PDF 数据
            page_num: 要识别的页码（从1开始）

        Returns:
            包含识别文本的结果字典
        """
        token = self._get_access_token()

        resp = requests.post(
            f"{self.ocr_url}?access_token={token}",
            data={
                "pdf_file": pdf_base64,
                "pdf_file_num": str(page_num),
                "language_type": "CHN_ENG",
                "detect_direction": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())

    def recognize_pdf_bytes(self, pdf_bytes: bytes, page_num: int = 1) -> Dict[str, Any]:
        """
        识别 PDF 文件中的文字（bytes 输入，内部转为 base64 后调用 recognize_pdf）

        Args:
            pdf_bytes: 原始 PDF 字节数据
            page_num: 要识别的页码（从1开始）

        Returns:
            包含识别文本的结果字典
        """
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
        return self.recognize_pdf(pdf_base64, page_num)

    def recognize_pdf_multi_pages(self, pdf_bytes: bytes, max_pages: int = 6,
                                    early_stop_check=None) -> Dict[str, Any]:
        """
        识别 PDF 多页文字，逐页调用百度 OCR 并拼接结果。
        遇到条款页（大段条款文本）时自动停止。

        Args:
            pdf_bytes: 原始 PDF 字节数据
            max_pages: 最多识别的页数（默认6页）
            early_stop_check: 可选回调 fn(combined_text) -> bool，
                              返回 True 表示核心字段已提取完毕可以提前停止

        Returns:
            包含识别文本的结果字典
        """
        import io
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                total_pages = len(pdf.pages)
        except Exception:
            total_pages = max_pages

        pages_to_scan = min(max_pages, total_pages)
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

        all_texts = []
        for page in range(1, pages_to_scan + 1):
            try:
                result = self.recognize_pdf(pdf_base64, page_num=page)
                if result.get("success"):
                    page_text = result.get("text", "")
                    logger.info("百度OCR第%d/%d页识别成功，文字数=%d", page, pages_to_scan, len(page_text))
                    # 条款/广告页跳过（不加入文本），但继续扫描后续页（可能有新保单）
                    if page > 2 and _is_clause_page(page_text):
                        logger.info("百度OCR第%d页检测为条款页，跳过该页继续扫描", page)
                        continue
                    # 插入页码标记，供多保单拆分时追踪页码
                    all_texts.append(f"[PAGE:{page}]\n{page_text}")
                    # 扫过至少一半总页数后，才检查核心字段是否已提取完毕
                    min_pages_before_stop = max(3, total_pages // 2)
                    if page >= min_pages_before_stop and early_stop_check and len(all_texts) >= 2:
                        combined_so_far = "\n".join(all_texts)
                        if early_stop_check(combined_so_far):
                            logger.info("百度OCR第%d页后核心字段已齐全，提前停止扫描（总页数%d）", page, total_pages)
                            break
                else:
                    logger.warning("百度OCR第%d页识别失败: %s", page, result.get("error", ""))
            except Exception as e:
                logger.warning("百度OCR第%d页异常: %s", page, e)
                continue

        if not all_texts:
            return {"success": False, "error": "百度OCR多页识别均失败", "text": ""}

        combined = "\n".join(all_texts)
        return {
            "success": True,
            "text": combined,
            "lines": combined.split("\n"),
            "words_count": combined.count("\n") + 1,
            "char_count": len(combined),
            "ocr_engine": "baidu",
            "pages_scanned": len(all_texts),
        }

    def recognize_image(self, image_base64: str) -> Dict[str, Any]:
        """
        识别图片中的文字（base64 输入）

        Args:
            image_base64: Base64 编码的图片数据

        Returns:
            包含识别文本的结果字典
        """
        token = self._get_access_token()

        resp = requests.post(
            f"{self.ocr_url}?access_token={token}",
            data={
                "image": image_base64,
                "language_type": "CHN_ENG",
                "detect_direction": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())
