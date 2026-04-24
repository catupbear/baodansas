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

    def __init__(self, secret_id: str, secret_key: str, region: str, bucket: str, prefix: str = "wxbot/media/"):
        self.bucket = bucket
        self.prefix = prefix
        self.region = region

        config = CosConfig(
            Region=region,
            SecretId=secret_id,
            SecretKey=secret_key,
        )
        self.client = CosS3Client(config)
        logger.info("COS初始化完成: %s/%s", bucket, prefix)

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
            url = f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{cos_key}"
            logger.info("上传COS成功: %s", url)
            return url
        except Exception as e:
            logger.error("上传COS失败: %s", e)
            return ""

    def get_url(self, filename: str) -> str:
        """获取文件的COS URL"""
        cos_key = f"{self.prefix}{filename}"
        return f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{cos_key}"

    def file_exists(self, filename: str) -> bool:
        """检查文件是否已存在于COS"""
        cos_key = f"{self.prefix}{filename}"
        try:
            self.client.head_object(Bucket=self.bucket, Key=cos_key)
            return True
        except Exception:
            return False
