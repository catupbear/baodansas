"""
消息解密模块
RSA解密 encrypt_random_key → SDK DecryptData 解密消息体
"""

import base64
import logging

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from sdk.finance_sdk import FinanceSDK

logger = logging.getLogger(__name__)


class MessageDecryptor:
    """消息解密器"""

    def __init__(self, finance_sdk: FinanceSDK, private_key_path: str):
        self.finance_sdk = finance_sdk
        # 加载RSA私钥
        with open(private_key_path, "r") as f:
            self.private_key = RSA.import_key(f.read())
        self.cipher = PKCS1_v1_5.new(self.private_key)
        logger.info("RSA私钥加载成功: %s", private_key_path)

    def decrypt_message(self, encrypt_random_key: str, encrypt_chat_msg: str) -> tuple[int, str]:
        """
        解密单条消息
        Args:
            encrypt_random_key: RSA加密的随机密钥
            encrypt_chat_msg: 加密的消息内容
        返回: (错误码, 解密后的JSON字符串)
        """
        try:
            # 第一步：RSA解密 encrypt_random_key，得到消息密钥
            encrypted_key_bytes = base64.b64decode(encrypt_random_key)
            random_key = self.cipher.decrypt(encrypted_key_bytes, sentinel=None)
            if random_key is None:
                logger.error("RSA解密失败")
                return -1, ""
            random_key_str = random_key.decode("utf-8")

            # 第二步：用消息密钥调用SDK解密消息内容
            ret, decrypted_msg = self.finance_sdk.decrypt_data(random_key_str, encrypt_chat_msg)
            if ret != 0:
                logger.error("SDK解密消息失败, 错误码: %d", ret)
                return ret, ""

            return 0, decrypted_msg
        except Exception as e:
            logger.error("消息解密异常: %s", e)
            return -1, ""
