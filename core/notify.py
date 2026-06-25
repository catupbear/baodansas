"""
钉钉机器人错误通知模块
当系统发生异常时，通过钉钉机器人发送告警消息
"""

import hashlib
import hmac
import base64
import json
import logging
import time
import threading
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# 通知频率限制：相同错误 15 分钟内不重复发送
_DEDUP_INTERVAL = 900
_recent_errors = {}
_lock = threading.Lock()

# 全局单例
_notifier = None


class DingTalkNotifier:
    """钉钉机器人通知器"""

    def __init__(self, webhook_url: str, secret: str = ""):
        self.webhook_url = webhook_url
        self.secret = secret

    def _sign(self) -> str:
        """计算加签参数"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

    def send(self, title: str, content: str):
        """
        发送 Markdown 格式消息到钉钉群。
        自动去重：相同 title 在 5 分钟内不重复发送。
        """
        dedup_key = title
        now = time.time()
        with _lock:
            last_time = _recent_errors.get(dedup_key, 0)
            if now - last_time < _DEDUP_INTERVAL:
                return
            _recent_errors[dedup_key] = now
            # 清理过期的去重记录
            expired = [k for k, v in _recent_errors.items() if now - v > _DEDUP_INTERVAL]
            for k in expired:
                del _recent_errors[k]

        url = self._sign() if self.secret else self.webhook_url
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": content,
            },
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("errcode") != 0:
                    logger.warning("钉钉通知发送失败: %s", result)
        except Exception as e:
            logger.warning("钉钉通知发送异常: %s", e)


def init_notifier(webhook_url: str, secret: str = ""):
    """初始化全局通知器"""
    global _notifier
    if webhook_url:
        _notifier = DingTalkNotifier(webhook_url, secret)
        logger.info("钉钉错误通知已启用")


def notify_error(module: str, method: str, error: str, detail: str = ""):
    """
    发送错误通知（异步，不阻塞业务流程）。

    Args:
        module: 模块名称，如 "消息拉取"、"保单识别"
        method: 方法/API 名称，如 "fetch_new_messages"、"/api/insurance/upload"
        error:  错误摘要
        detail: 补充信息（可选）
    """
    if not _notifier:
        return

    title = f"⚠ {module} 异常"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"### ⚠ {module} 异常告警",
        f"- **时间**: {ts}",
        f"- **模块**: {module}",
        f"- **方法**: `{method}`",
        f"- **错误**: {error[:500]}",
    ]
    if detail:
        lines.append(f"- **详情**: {detail[:500]}")
    content = "\n".join(lines)

    # 异步发送，不阻塞主流程
    threading.Thread(
        target=_notifier.send, args=(title, content), daemon=True
    ).start()


def notify_new_company(company: str, detail: str = "", webhook: str = "", secret: str = ""):
    """
    发现规则外的新保司时通知（钉钉，新保司专用群）。

    使用独立 webhook（与系统错误告警分开）；未配置 webhook 则不发送。

    Args:
        company: 识别出的保司全称（可能为空，表示连全称都没归出来）
        detail:  补充信息（文件名、record_id、车牌等）
        webhook: 新保司专用钉钉群机器人 webhook
        secret:  加签密钥（可选）
    """
    if not webhook:
        return

    title = f"🆕 发现新保司: {company or '未识别'}"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### 🆕 发现规则外的新保司",
        f"- **保司**: {company or '（未能识别出保司名称）'}",
        f"- **时间**: {ts}",
    ]
    if detail:
        lines.append(f"- **详情**: {detail[:500]}")
    lines.append("")
    lines.append("> 系统识别为保单但归不出保司简称，请在 `policy_parser.py` 补充识别规则"
                 "（COMPANY_SHORT_MAP 简称 + COMPANY_BASES 全称/前缀）。")
    content = "\n".join(lines)

    notifier = DingTalkNotifier(webhook, secret)
    threading.Thread(
        target=notifier.send, args=(title, content), daemon=True
    ).start()


def notify_error_report(description: str, detail: str = "", webhook: str = "", secret: str = ""):
    """
    用户提交识别报错反馈时通知钉钉群（异步，不阻塞业务）。

    复用「新保司通知」群的 webhook/secret（由调用方传入）；未配置则不发送。

    Args:
        description: 用户填写的问题描述
        detail:      补充信息（反馈人、企业、文件名、保单号、record_id 等）
        webhook:     钉钉群机器人 webhook
        secret:      加签密钥（可选）
    """
    if not webhook:
        return

    title = "⚠️ 用户报错反馈"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### ⚠️ 用户提交识别报错反馈",
        f"- **时间**: {ts}",
    ]
    if detail:
        lines.append(f"- **记录**: {detail[:500]}")
    lines.append(f"- **问题描述**: {(description or '')[:1000]}")
    content = "\n".join(lines)

    notifier = DingTalkNotifier(webhook, secret)
    threading.Thread(
        target=notifier.send, args=(title, content), daemon=True
    ).start()
