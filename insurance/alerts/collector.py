"""低匹配保单自动收集 — 积累样本后触发训练"""

import logging

from insurance.db import get_insurance_config, set_insurance_config

logger = logging.getLogger(__name__)


class LowMatchCollector:
    """
    收集匹配度低于阈值的保单文本样本。
    当样本积累到一定数量后，返回就绪信号以触发模板训练。
    """

    THRESHOLD = 0.6    # 低于此匹配度的保单被收集
    MIN_SAMPLES = 3    # 积累到此数量触发训练
    MAX_TEXT_LEN = 5000  # 每个样本最多保留的字符数

    def __init__(self, db):
        self.db = db

    def _config_key(self, company_id: str, policy_type: str) -> str:
        """生成配置存储的 key"""
        return f"low_match_samples_{company_id}_{policy_type}"

    def collect(self, company_id: str, policy_type: str, text: str,
                match_score: float, record_id: int = None) -> dict:
        """
        收集低匹配样本。
        Args:
            company_id: 保司 ID
            policy_type: 险种代码
            text: OCR 提取的保单文本
            match_score: 模板匹配度
            record_id: 关联的保单记录 ID
        Returns:
            达到训练条件时返回 {"ready": True, "company_id": ..., "policy_type": ..., "sample_count": ...}
            否则返回 None
        """
        # 匹配度高于阈值，不需要收集
        if match_score >= self.THRESHOLD:
            return None

        key = self._config_key(company_id, policy_type)

        # 读取已收集的样本列表
        samples = get_insurance_config(self.db, key, default=[])
        if not isinstance(samples, list):
            samples = []

        # 追加当前样本（截断到最大长度）
        sample_entry = {
            "text": text[:self.MAX_TEXT_LEN],
            "match_score": match_score,
            "record_id": record_id,
        }
        samples.append(sample_entry)

        # 保存回数据库
        set_insurance_config(self.db, key, samples)

        sample_count = len(samples)
        logger.info(
            "低匹配样本已收集, company=%s, type=%s, score=%.2f, 累计=%d/%d",
            company_id, policy_type, match_score, sample_count, self.MIN_SAMPLES,
        )

        # 判断是否达到训练条件
        if sample_count >= self.MIN_SAMPLES:
            return {
                "ready": True,
                "company_id": company_id,
                "policy_type": policy_type,
                "sample_count": sample_count,
            }
        return None

    def get_samples(self, company_id: str, policy_type: str) -> list:
        """获取收集的样本文本列表"""
        key = self._config_key(company_id, policy_type)
        samples = get_insurance_config(self.db, key, default=[])
        if not isinstance(samples, list):
            return []
        return [s.get("text", "") if isinstance(s, dict) else str(s) for s in samples]

    def clear_samples(self, company_id: str, policy_type: str):
        """清除样本（训练完成后调用）"""
        key = self._config_key(company_id, policy_type)
        set_insurance_config(self.db, key, [])
        logger.info("低匹配样本已清除, company=%s, type=%s", company_id, policy_type)
