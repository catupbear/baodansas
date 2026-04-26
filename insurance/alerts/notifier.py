"""告警通知 — 数据库记录 + 钉钉推送"""

import logging
import requests

from insurance.engine.schema import Alert
from insurance.db import save_alert_record

logger = logging.getLogger(__name__)


class AlertNotifier:
    """告警通知器：将告警存入数据库，并对 warning/critical 级别推送钉钉群机器人"""

    def __init__(self, db, dingtalk_webhook: str = ""):
        self.db = db
        self.dingtalk_webhook = dingtalk_webhook

    def notify(self, alert: Alert, context: dict, insurance_record_id: int = None):
        """
        发送告警通知。
        Args:
            alert: Alert 数据对象
            context: 上下文信息，包含 company_name / policy_type / filename 等
            insurance_record_id: 关联的保单记录 ID（可选）
        """
        # 1. 存数据库
        self._save_to_db(alert, context, insurance_record_id)

        # 2. warning/critical 级别推送钉钉
        if alert.level in ("warning", "critical") and self.dingtalk_webhook:
            self._send_dingtalk(alert, context)

    def _save_to_db(self, alert: Alert, context: dict, insurance_record_id: int = None):
        """将告警记录写入数据库，失败只记日志不抛异常"""
        try:
            record = {
                "insurance_record_id": insurance_record_id,
                "template_id": alert.template_id,
                "match_score": context.get("match_score", 0.0),
                "alert_level": alert.level,
                "alert_type": alert.alert_type,
                "alert_message": alert.message,
            }
            alert_id = save_alert_record(self.db, record)
            logger.info("告警已保存, id=%d, 级别=%s, 类型=%s", alert_id, alert.level, alert.alert_type)
        except Exception:
            logger.exception("保存告警记录失败")

    def _send_dingtalk(self, alert: Alert, context: dict):
        """构造消息并推送到钉钉群机器人 webhook"""
        try:
            # 级别标签
            level_label = {"warning": "⚠️ 警告", "critical": "🚨 严重"}.get(alert.level, alert.level)

            # 构造消息文本
            lines = [
                f"【保单告警 {level_label}】",
                f"保司：{context.get('company_name', '未知')}",
                f"险种：{context.get('policy_type', '未知')}",
                f"模板：{alert.template_id or '无'}",
                f"文件：{context.get('filename', '未知')}",
                f"问题：{alert.message}",
            ]
            text = "\n".join(lines)

            payload = {
                "msgtype": "text",
                "text": {"content": text},
            }
            resp = requests.post(self.dingtalk_webhook, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("钉钉告警推送成功, 级别=%s", alert.level)
            else:
                logger.warning("钉钉告警推送失败, status=%d, body=%s", resp.status_code, resp.text)
        except Exception:
            logger.exception("钉钉告警推送异常")
