# quote/binding.py
"""
微信群-保险账号绑定关系查询
调用外部接口获取监控列表，1分钟缓存
"""

import time
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://riskapi.wuhuxiche.com"


class BindingService:
    """微信绑定关系服务"""

    def __init__(self):
        self._cache: List[Dict] = []
        self._cache_time: float = 0
        self._cache_ttl: int = 60  # 缓存1分钟

    def _fetch_bindings(self) -> List[Dict]:
        """从远程接口获取绑定列表"""
        try:
            resp = requests.get(
                f"{BASE_URL}/insurance/wechat-bindings/all",
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") != 0:
                logger.error("获取绑定列表失败: %s", result.get("msg", "未知错误"))
                return []
            return result.get("data", [])
        except Exception as e:
            logger.error("请求绑定列表异常: %s", e)
            return []

    def get_bindings(self) -> List[Dict]:
        """获取绑定列表（带缓存）"""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            logger.info("报价绑定列表：使用缓存（剩余 %ds），共 %d 条",
                        int(self._cache_ttl - (now - self._cache_time)), len(self._cache))
            return self._cache
        self._cache = self._fetch_bindings()
        self._cache_time = now
        logger.info("报价绑定列表：从接口刷新，共 %d 条", len(self._cache))
        for b in self._cache:
            logger.info("  绑定: id=%s, group_id=%s, group_name=%s, wechat_id=%s, "
                        "platform_user_id=%s, auto_quote=%s",
                        b.get("id"), b.get("group_id"), b.get("group_name"),
                        b.get("wechat_id"), b.get("platform_user_id"), b.get("auto_quote"))
        return self._cache

    def find_binding(self, group_id: str, sender: str) -> Optional[Dict]:
        """
        查找匹配的绑定记录
        Args:
            group_id: 微信群ID（roomid）
            sender: 发送人微信ID
        Returns:
            匹配的绑定记录，未匹配返回 None
        """
        bindings = self.get_bindings()
        for b in bindings:
            b_group_id = (b.get("group_id") or "").strip()
            b_wechat_id = (b.get("wechat_id") or "").strip()
            if b_group_id == group_id and b_wechat_id == sender:
                return b
        return None
