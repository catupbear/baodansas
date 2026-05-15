"""
企业微信回调消息加解密工具
基于官方 WXBizMsgCrypt 方案实现
"""

import base64
import hashlib
import socket
import struct
import time
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES


class WXBizMsgCrypt:
    """企业微信回调消息加解密类"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey 为 43 位字符，Base64 解码后得到 32 字节 AES 密钥
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> tuple[int, str]:
        """
        验证回调URL有效性（GET请求）
        返回: (错误码, 解密后的echostr)  错误码0表示成功
        """
        # 验证签名
        if not self._check_signature(msg_signature, timestamp, nonce, echostr):
            return -1, ""
        # 解密 echostr
        ret, content = self._decrypt(echostr)
        return ret, content

    def decrypt_msg(self, post_data: str, msg_signature: str, timestamp: str, nonce: str) -> tuple[int, str]:
        """
        解密回调POST消息
        返回: (错误码, 解密后的XML明文)
        """
        try:
            tree = ET.fromstring(post_data)
            encrypt = tree.find("Encrypt").text
        except Exception:
            return -1, ""

        # 验证签名
        if not self._check_signature(msg_signature, timestamp, nonce, encrypt):
            return -1, ""

        # 解密
        ret, content = self._decrypt(encrypt)
        return ret, content

    def encrypt_msg(self, reply_msg: str, nonce: str, timestamp: str = None) -> tuple[int, str]:
        """
        加密回复消息
        返回: (错误码, 加密后的XML)
        """
        if timestamp is None:
            timestamp = str(int(time.time()))

        # 加密
        encrypt = self._encrypt(reply_msg)

        # 生成签名
        signature = self._generate_signature(timestamp, nonce, encrypt)

        # 拼装XML
        resp_xml = (
            "<xml>\n"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>\n"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>\n"
            f"<TimeStamp>{timestamp}</TimeStamp>\n"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>\n"
            "</xml>"
        )
        return 0, resp_xml

    def _check_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        """校验签名"""
        signature = self._generate_signature(timestamp, nonce, encrypt)
        if signature != msg_signature:
            import logging
            logging.getLogger(__name__).warning(
                "签名不匹配: 收到=%s, 计算=%s, token=%s, encrypt长度=%d",
                msg_signature, signature, self.token, len(encrypt),
            )
        return signature == msg_signature

    def _generate_signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        """生成签名"""
        sort_list = sorted([self.token, timestamp, nonce, encrypt])
        sha1 = hashlib.sha1("".join(sort_list).encode("utf-8")).hexdigest()
        return sha1

    def _encrypt(self, text: str) -> str:
        """AES加密"""
        # 拼接：16字节随机字符串 + 4字节网络字节序消息长度 + 明文 + corpid
        text_bytes = text.encode("utf-8")
        corp_id_bytes = self.corp_id.encode("utf-8")
        # 16字节随机字符串
        random_str = self._get_random_str().encode("utf-8")
        # 组装明文
        plain = random_str + struct.pack("!I", len(text_bytes)) + text_bytes + corp_id_bytes
        # PKCS#7 填充
        pad_len = 32 - (len(plain) % 32)
        plain += bytes([pad_len] * pad_len)
        # AES-CBC 加密
        iv = self.aes_key[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        encrypted = cipher.encrypt(plain)
        return base64.b64encode(encrypted).decode("utf-8")

    def _decrypt(self, encrypt: str) -> tuple[int, str]:
        """AES解密"""
        try:
            cipher_text = base64.b64decode(encrypt)
            iv = self.aes_key[:16]
            cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
            plain = cipher.decrypt(cipher_text)
            # 去除 PKCS#7 填充
            pad_len = plain[-1]
            plain = plain[:-pad_len]
            # 解析：16字节随机字符串 + 4字节长度 + 明文 + corpid
            msg_len = struct.unpack("!I", plain[16:20])[0]
            content = plain[20:20 + msg_len].decode("utf-8")
            from_corp_id = plain[20 + msg_len:].decode("utf-8")
            if from_corp_id != self.corp_id:
                return -1, ""
            return 0, content
        except Exception as e:
            return -1, str(e)

    @staticmethod
    def _get_random_str() -> str:
        """生成16位随机字符串"""
        import random
        import string
        return "".join(random.choices(string.ascii_letters + string.digits, k=16))
