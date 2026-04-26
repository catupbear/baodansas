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


class BaiduOCR:
    """百度OCR客户端"""

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
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
            f"{self.OCR_URL}?access_token={token}",
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
            f"{self.OCR_URL}?access_token={token}",
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
