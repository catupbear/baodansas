"""百度 OCR 引擎 — 通用文字识别（高精度版）"""

import base64
import time
from typing import Optional

import requests

from .base import BaseOCREngine, OCRResult


class BaiduOCREngine(BaseOCREngine):
    """
    百度 OCR 引擎，使用通用文字识别（高精度版）API
    API 文档: https://cloud.baidu.com/doc/OCR/s/1k3h7y3db
    """

    engine_name = "baidu"
    engine_type = "image"

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
        # token 有效期通常30天，提前1小时标记为过期
        self._token_expire_time = time.time() + result.get("expires_in", 2592000) - 3600
        return self._access_token

    def extract(self, pdf_bytes: bytes, page: int = 1) -> OCRResult:
        """
        识别 PDF 中的文字

        Args:
            pdf_bytes: PDF 原始字节数据
            page: 要识别的页码（从1开始）

        Returns:
            OCRResult，默认 confidence=0.9
        """
        start = time.time()
        try:
            token = self._get_access_token()
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

            resp = requests.post(
                f"{self.OCR_URL}?access_token={token}",
                data={
                    "pdf_file": pdf_base64,
                    "pdf_file_num": str(page),
                    "language_type": "CHN_ENG",
                    "detect_direction": "true",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            # 接口返回错误码
            if "error_code" in result:
                return OCRResult(
                    engine=self.engine_name,
                    success=False,
                    error=f"百度OCR识别失败: {result.get('error_msg', '未知错误')} (错误码: {result['error_code']})",
                    cost_ms=int((time.time() - start) * 1000),
                )

            words_list = result.get("words_result", [])
            if not words_list:
                return OCRResult(
                    engine=self.engine_name,
                    success=False,
                    error="百度OCR未识别到任何文字",
                    cost_ms=int((time.time() - start) * 1000),
                )

            text = "\n".join(item["words"] for item in words_list)
            return OCRResult(
                engine=self.engine_name,
                success=True,
                text=text,
                confidence=0.9,
                cost_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return OCRResult(
                engine=self.engine_name,
                success=False,
                error=f"百度OCR异常: {e}",
                cost_ms=int((time.time() - start) * 1000),
            )
