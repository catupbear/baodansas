"""
腾讯云 COS 存储模块
媒体文件下载后上传到 COS，返回可访问的 URL
"""

import hashlib
import logging
import os

from qcloud_cos import CosConfig, CosS3Client

logger = logging.getLogger(__name__)


class CosStorage:
    """腾讯云COS存储"""

    def __init__(self, secret_id: str, secret_key: str, region: str, bucket: str,
                 prefix: str = "wxbot/media/", cdn_domain: str = ""):
        self.bucket = bucket
        self.prefix = prefix
        self.region = region
        # CDN 域名（如 wxbotcdn.wuhuxiche.com），为空时使用默认 COS 域名
        self.cdn_domain = cdn_domain.rstrip("/") if cdn_domain else ""

        config = CosConfig(
            Region=region,
            SecretId=secret_id,
            SecretKey=secret_key,
        )
        self.client = CosS3Client(config)
        cdn_info = f", cdn={self.cdn_domain}" if self.cdn_domain else ""
        logger.info("COS初始化完成: %s/%s%s", bucket, prefix, cdn_info)

    def _make_url(self, cos_key: str) -> str:
        """根据是否配置 CDN 域名生成访问 URL"""
        if self.cdn_domain:
            return f"https://{self.cdn_domain}/{cos_key}"
        return f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{cos_key}"

    def upload_file(self, local_path: str, filename: str) -> str:
        """
        上传文件到COS
        Args:
            local_path: 本地文件路径
            filename: COS上的文件名
        返回: 文件的访问URL
        """
        cos_key = f"{self.prefix}{filename}"
        try:
            self.client.upload_file(
                Bucket=self.bucket,
                Key=cos_key,
                LocalFilePath=local_path,
            )
            url = self._make_url(cos_key)
            logger.info("上传COS成功: %s", url)
            return url
        except Exception as e:
            logger.error("上传COS失败: %s", e)
            return ""

    def upload_bytes(self, data: bytes, cos_key_suffix: str, download_filename: str = "") -> str:
        """
        直接上传字节流到COS（不落盘）
        Args:
            data: 文件字节流
            cos_key_suffix: COS 上的相对路径（如 insurance/roomid/sender/file.pdf）
            download_filename: 下载时保存的文件名（可选，支持中文）。COS key 保持
                ASCII 避免 URL 含中文被微信/浏览器截断链接，下载文件名通过对象的
                Content-Disposition 响应头指定
        返回: 文件的访问URL
        """
        cos_key = f"{self.prefix}{cos_key_suffix}"
        try:
            extra = {}
            if download_filename:
                from urllib.parse import quote
                extra["ContentDisposition"] = (
                    f"attachment; filename*=UTF-8''{quote(download_filename)}"
                )
            self.client.put_object(
                Bucket=self.bucket,
                Key=cos_key,
                Body=data,
                **extra,
            )
            url = self._make_url(cos_key)
            logger.info("上传COS成功(bytes): %s", url)
            return url
        except Exception as e:
            logger.error("上传COS失败(bytes): %s", e)
            return ""

    def get_url(self, filename: str) -> str:
        """获取文件的COS URL"""
        cos_key = f"{self.prefix}{filename}"
        return self._make_url(cos_key)

    def file_exists(self, filename: str) -> bool:
        """检查文件是否已存在于COS"""
        cos_key = f"{self.prefix}{filename}"
        try:
            self.client.head_object(Bucket=self.bucket, Key=cos_key)
            return True
        except Exception:
            return False
