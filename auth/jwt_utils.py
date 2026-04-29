"""
JWT Token 工具
负责 token 的签发与验证
"""

import secrets
import time
import logging

import jwt

logger = logging.getLogger(__name__)

# 模块级配置，由 main.py 初始化时设置（默认生成随机密钥，防止使用弱密钥）
_secret_key = secrets.token_hex(32)
_expire_seconds = 86400 * 7  # 默认 7 天


def init_jwt(secret_key: str, expire_seconds: int = 86400 * 7):
    """初始化 JWT 配置"""
    global _secret_key, _expire_seconds
    _secret_key = secret_key
    _expire_seconds = expire_seconds


def generate_token(user_id: int, username: str, role: str) -> str:
    """签发 JWT token"""
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": int(time.time()) + _expire_seconds,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, _secret_key, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """
    验证并解码 token。
    成功返回 payload dict，失败返回 None。
    """
    try:
        payload = jwt.decode(token, _secret_key, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug("Token 无效: %s", e)
        return None
