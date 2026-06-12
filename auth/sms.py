"""
短信发送模块 — 阿里云 SMS
"""
import json
import logging
import random
import string

logger = logging.getLogger(__name__)

_sms_config = {}


def init_sms(config: dict):
    """注入短信配置（从 config.yaml 的 sms 节点读取）"""
    global _sms_config
    _sms_config = config or {}


def generate_code(length: int = 6) -> str:
    """生成纯数字验证码"""
    return "".join(random.choices(string.digits, k=length))


def send_sms_code(phone: str, code: str) -> tuple[bool, str]:
    """
    发送验证码短信。
    返回 (success: bool, error_msg: str)
    """
    cfg = _sms_config
    if not cfg.get("access_key_id"):
        # 未配置短信服务，打印验证码到日志（开发/测试用）
        logger.info("[SMS-DEV] 手机号 %s 验证码: %s", phone, code)
        return True, ""

    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525 import models as sms_models
        from alibabacloud_tea_openapi import models as open_api_models

        open_cfg = open_api_models.Config(
            access_key_id=cfg["access_key_id"],
            access_key_secret=cfg["access_key_secret"],
        )
        open_cfg.endpoint = "dysmsapi.aliyuncs.com"
        client = Client(open_cfg)

        req = sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=cfg["sign_name"],
            template_code=cfg["template_code"],
            template_param=json.dumps({"code": code}),
        )
        resp = client.send_sms(req)
        if resp.body.code == "OK":
            logger.info("[SMS] 验证码已发送 → %s", phone)
            return True, ""
        err = resp.body.message or resp.body.code or "未知错误"
        logger.warning("[SMS] 发送失败: %s → %s", phone, err)
        return False, err
    except Exception as e:
        logger.exception("[SMS] 发送异常: %s", e)
        return False, str(e)
