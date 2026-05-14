# quote/ocr.py
"""
百度OCR - 行驶证识别 & 身份证识别
行驶证API: https://cloud.baidu.com/doc/OCR/s/yk3h7y3ks
身份证API: https://cloud.baidu.com/doc/OCR/s/rk3h7xzck
"""

import base64
import time
import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class QuoteOCR:
    """保险报价证件OCR识别"""

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    VEHICLE_LICENSE_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_license"
    IDCARD_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/idcard"

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
        self._token_expire_time = time.time() + result.get("expires_in", 2592000) - 3600
        return self._access_token

    def recognize_vehicle_license(self, image_bytes: bytes = None, *, image_url: str = None) -> Dict[str, Any]:
        """
        识别行驶证
        Args:
            image_bytes: 图片二进制数据（与 image_url 二选一）
            image_url: 图片URL（与 image_bytes 二选一，优先使用）
        Returns:
            成功: {"success": True, "type": "vehicle_license", "data": {字段...}}
            失败: {"success": False, "type": "vehicle_license", "error": "原因"}
        """
        token = self._get_access_token()

        if image_url:
            data = {"url": image_url, "detect_direction": "true"}
        else:
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            data = {"image": image_base64, "detect_direction": "true"}

        try:
            resp = requests.post(
                f"{self.VEHICLE_LICENSE_URL}?access_token={token}",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info("行驶证OCR原始返回: %s", result)

            if "error_code" in result:
                return {
                    "success": False,
                    "type": "vehicle_license",
                    "error": f"错误码{result['error_code']}: {result.get('error_msg', '')}",
                }

            words = result.get("words_result", {})
            if not words:
                return {"success": False, "type": "vehicle_license", "error": "未识别到行驶证字段"}

            plate = words.get("号牌号码", {}).get("words", "")
            vin = words.get("车辆识别代号", {}).get("words", "")
            owner = words.get("所有人", {}).get("words", "")

            # 至少要有车牌号或车架号才认为识别成功
            if not plate and not vin:
                return {"success": False, "type": "vehicle_license", "error": "缺少车牌号和车架号，非有效行驶证"}

            def _fmt_date(s: str) -> str:
                """将 YYYYMMDD 格式化为 YYYY-MM-DD"""
                s = s.strip().replace("/", "").replace("-", "").replace(".", "")
                if len(s) == 8 and s.isdigit():
                    return f"{s[:4]}-{s[4:6]}-{s[6:]}"
                return s

            return {
                "success": True,
                "type": "vehicle_license",
                "data": {
                    "plate_number": plate,
                    "vin": vin,
                    "engine_number": words.get("发动机号码", {}).get("words", ""),
                    "first_register_date": _fmt_date(words.get("注册日期", {}).get("words", "")),
                    "issue_date": _fmt_date(words.get("发证日期", {}).get("words", "")),
                    "model": words.get("品牌型号", {}).get("words", ""),
                    "owner_name": owner,  # 用于姓名配对，不提交到 pre-quote
                },
            }
        except requests.RequestException as e:
            return {"success": False, "type": "vehicle_license", "error": str(e)}

    def recognize_idcard(self, image_bytes: bytes = None, *, image_url: str = None) -> Dict[str, Any]:
        """
        识别身份证（先尝试正面，失败再尝试反面）
        Args:
            image_bytes: 图片二进制数据（与 image_url 二选一）
            image_url: 图片URL（与 image_bytes 二选一，优先使用）
        Returns:
            成功: {"success": True, "type": "idcard", "side": "front/back", "data": {字段...}}
            失败: {"success": False, "type": "idcard", "error": "原因"}
        """
        token = self._get_access_token()

        if image_url:
            image_data = {"url": image_url}
        else:
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            image_data = {"image": image_base64}

        front_result = self._call_idcard(token, image_data, "front")
        if front_result.get("success"):
            return front_result

        back_result = self._call_idcard(token, image_data, "back")
        if back_result.get("success"):
            return back_result

        return {"success": False, "type": "idcard", "error": "正反面均未识别到有效身份证信息"}

    def _call_idcard(self, token: str, image_data: dict, side: str) -> Dict[str, Any]:
        """调用身份证识别API
        Args:
            image_data: {"url": "..."} 或 {"image": "base64..."}
        """
        try:
            data = {**image_data, "id_card_side": side, "detect_direction": "true"}
            resp = requests.post(
                f"{self.IDCARD_URL}?access_token={token}",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info("身份证OCR原始返回(side=%s): %s", side, result)

            if "error_code" in result:
                return {
                    "success": False,
                    "type": "idcard",
                    "error": f"错误码{result['error_code']}: {result.get('error_msg', '')}",
                }

            words = result.get("words_result", {})
            if not words:
                return {"success": False, "type": "idcard", "error": f"身份证{side}未识别到字段"}

            if side == "front":
                name = words.get("姓名", {}).get("words", "")
                id_number = words.get("公民身份号码", {}).get("words", "")
                if not name and not id_number:
                    return {"success": False, "type": "idcard", "error": "缺少姓名和身份证号，非有效身份证正面"}
                return {
                    "success": True,
                    "type": "idcard",
                    "side": "front",
                    "data": {
                        "name": name,
                        "id_number": id_number,
                    },
                }
            else:  # back
                issue_date = words.get("签发日期", {}).get("words", "")
                expiry_date = words.get("失效日期", {}).get("words", "")
                if not issue_date and not expiry_date:
                    return {"success": False, "type": "idcard", "error": "缺少签发/失效日期，非有效身份证反面"}
                return {
                    "success": True,
                    "type": "idcard",
                    "side": "back",
                    "data": {
                        "id_card_start_date": issue_date,
                        "id_card_end_date": expiry_date,
                    },
                }
        except requests.RequestException as e:
            return {"success": False, "type": "idcard", "error": str(e)}
