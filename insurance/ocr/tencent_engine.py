"""腾讯云 OCR 引擎 — 通用印刷体识别（高精度版）"""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import requests

from .base import BaseOCREngine, OCRResult


class TencentOCREngine(BaseOCREngine):
    """
    腾讯云 OCR 引擎，使用 GeneralAccurateOCR API
    通过 requests 直接调用腾讯云 API v3（手动 TC3-HMAC-SHA256 签名，不依赖 SDK）
    API 文档: https://cloud.tencent.com/document/product/866/34937
    """

    engine_name = "tencent"
    engine_type = "image"

    API_HOST = "ocr.tencentcloudapi.com"
    API_URL = f"https://{API_HOST}"
    SERVICE = "ocr"
    ACTION = "GeneralAccurateOCR"
    VERSION = "2018-11-19"

    def __init__(self, secret_id: str, secret_key: str):
        self.secret_id = secret_id
        self.secret_key = secret_key

    def _sign(self, payload: str, timestamp: int, date: str) -> str:
        """
        TC3-HMAC-SHA256 签名

        Args:
            payload: 请求体 JSON 字符串
            timestamp: Unix 时间戳
            date: 日期字符串 YYYY-MM-DD

        Returns:
            完整的 Authorization 头值
        """
        # 步骤1: 拼接规范请求串
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = (
            f"content-type:application/json; charset=utf-8\n"
            f"host:{self.API_HOST}\n"
            f"x-tc-action:{self.ACTION.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = (
            f"{http_request_method}\n{canonical_uri}\n{canonical_querystring}\n"
            f"{canonical_headers}\n{signed_headers}\n{hashed_payload}"
        )

        # 步骤2: 拼接待签名字符串
        algorithm = "TC3-HMAC-SHA256"
        credential_scope = f"{date}/{self.SERVICE}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

        # 步骤3: 计算签名
        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac_sha256(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = _hmac_sha256(secret_date, self.SERVICE)
        secret_signing = _hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        # 步骤4: 拼接 Authorization
        authorization = (
            f"{algorithm} "
            f"Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        return authorization

    def extract(self, pdf_bytes: bytes, page: int = 1) -> OCRResult:
        """
        识别 PDF 中的文字

        Args:
            pdf_bytes: PDF 原始字节数据
            page: 要识别的页码（从1开始）

        Returns:
            OCRResult，置信度取各文本块平均值
        """
        start = time.time()
        try:
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

            # 构造请求体
            payload_dict = {
                "IsPdf": True,
                "PdfPageNumber": page,
                "ImageBase64": pdf_base64,
            }
            payload = json.dumps(payload_dict)

            # 签名
            timestamp = int(time.time())
            date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            authorization = self._sign(payload, timestamp, date)

            # 发送请求
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Host": self.API_HOST,
                "X-TC-Action": self.ACTION,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Version": self.VERSION,
                "Authorization": authorization,
            }
            resp = requests.post(self.API_URL, data=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            # 检查错误
            response_data = result.get("Response", {})
            error = response_data.get("Error")
            if error:
                return OCRResult(
                    engine=self.engine_name,
                    success=False,
                    error=f"腾讯云OCR失败: {error.get('Message', '未知错误')} (Code: {error.get('Code', '')})",
                    cost_ms=int((time.time() - start) * 1000),
                )

            # 解析 TextDetections
            detections = response_data.get("TextDetections", [])
            if not detections:
                return OCRResult(
                    engine=self.engine_name,
                    success=False,
                    error="腾讯云OCR未识别到任何文字",
                    cost_ms=int((time.time() - start) * 1000),
                )

            # 提取文本和置信度
            lines = []
            total_confidence = 0.0
            for det in detections:
                lines.append(det.get("DetectedText", ""))
                total_confidence += det.get("Confidence", 0.0)

            text = "\n".join(lines)
            avg_confidence = total_confidence / len(detections) / 100.0  # 腾讯返回 0-100

            return OCRResult(
                engine=self.engine_name,
                success=True,
                text=text,
                confidence=min(avg_confidence, 1.0),
                cost_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return OCRResult(
                engine=self.engine_name,
                success=False,
                error=f"腾讯云OCR异常: {e}",
                cost_ms=int((time.time() - start) * 1000),
            )
