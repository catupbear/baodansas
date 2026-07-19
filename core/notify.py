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

    def send(self, title: str, content: str, dedup: bool = True):
        """
        发送 Markdown 格式消息到钉钉群。
        自动去重：相同 title 在 15 分钟内不重复发送（dedup=False 时逐条发送不去重，
        用于消息转发类通知，每条内容都不同、不能被 title 去重吞掉）。
        """
        if dedup:
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


def notify_recovery(module: str, message: str, detail: str = ""):
    """
    发送恢复通知（复用系统告警机器人，✅ 口径，异步不阻塞）。

    与 notify_error 配对使用：异常告警后故障解除时，推送恢复消息闭环。
    去重规则沿用 DingTalkNotifier.send（同 title 15 分钟内不重复）。
    """
    if not _notifier:
        return

    title = f"✅ {module} 已恢复"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"### ✅ {module} 已恢复",
        f"- **时间**: {ts}",
        f"- **说明**: {message[:500]}",
    ]
    if detail:
        lines.append(f"- **详情**: {detail[:500]}")
    content = "\n".join(lines)

    threading.Thread(
        target=_notifier.send, args=(title, content), daemon=True
    ).start()


def notify_unmonitored_group(room_desc: str, sender_desc: str, content_desc: str):
    """
    未开通AI机器人识别的群收到补充信息/保单文件、且群内提醒发不出去
    （群名解析不到或FlowBot发送失败）时，钉钉告警兜底通知技术客服。

    复用系统错误告警的钉钉机器人；title 含群标识，同群 15 分钟内去重。
    """
    if not _notifier:
        return

    title = f"📣 未开通群消息提醒 {room_desc}"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### 📣 未开通群收到待识别消息",
        f"- **时间**: {ts}",
        f"- **群**: {room_desc}",
        f"- **发送人**: {sender_desc or '未知'}",
        f"- **内容**: {content_desc[:300]}",
        "",
        "> 该群尚未开通AI机器人识别，且群内提醒发送失败（群名无法解析或机器人不在群内），请尽快确认是否开通并完成配置。",
    ]
    content = "\n".join(lines)

    threading.Thread(
        target=_notifier.send, args=(title, content), daemon=True
    ).start()


def notify_fill_pending_miss(room_desc: str, sender_desc: str, plate: str):
    """
    群内补充信息延迟提醒（待匹配池超时仍未匹配到保单）时，钉钉同步通知技术客服。

    与 FlowBot 群内「未录入」提醒同时发出，复用系统错误告警的钉钉机器人。
    """
    if not _notifier:
        return

    title = f"⚠️ 补充信息未录入 {plate}"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### ⚠️ 群内补充信息未录入（超时未匹配）",
        f"- **时间**: {ts}",
        f"- **群**: {room_desc or '未知'}",
        f"- **发送人**: {sender_desc or '未知'}",
        f"- **车牌**: {plate or '未知'}",
        "",
        "> 该车牌的补充信息已等待超时仍未匹配到保单识别记录（已在群内提醒客户）。"
        "请核实保单是否已发群、车牌是否有误；后续保单识别成功会自动补录。",
    ]
    content = "\n".join(lines)

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


def notify_ai_check(title: str, content: str, webhook: str = "", secret: str = ""):
    """
    AI质检相关钉钉通知（差异提醒/成本提醒，异步不阻塞）。

    webhook 由系统设置「AI 模型」页配置；留空时回退复用系统告警机器人(_notifier)。
    去重规则沿用 DingTalkNotifier.send（同 title 15 分钟内不重复）。
    """
    if webhook:
        notifier = DingTalkNotifier(webhook, secret)
    elif _notifier:
        notifier = _notifier
    else:
        return
    threading.Thread(target=notifier.send, args=(title, content), daemon=True).start()


# 群文本消息转发通知器（独立 webhook，与系统错误告警机器人分开）
_text_msg_notifier = None


def init_text_msg_notifier(webhook_url: str, secret: str = ""):
    """初始化群文本消息转发通知器（外部联系人群聊文本 → 钉钉）"""
    global _text_msg_notifier
    if webhook_url:
        _text_msg_notifier = DingTalkNotifier(webhook_url, secret)
        logger.info("群文本消息钉钉转发已启用")


def text_msg_notify_enabled() -> bool:
    """群文本消息转发是否已启用（调用方先判断，避免白做通讯录解析）"""
    return _text_msg_notifier is not None


def notify_group_text(room_name: str, sender_name: str, text: str,
                      enterprise: str = "", msg_time: str = ""):
    """
    群内普通聊天文本转发到钉钉（异步不阻塞）。

    与错误告警不同：逐条发送、不做 title 去重（每条消息内容都不同）。

    Args:
        room_name:   消息所在群名（解析失败时为 roomid）
        sender_name: 发送人名称（解析失败时为原始 userid）
        text:        消息文本内容
        enterprise:  归属企业名（多企业部署时区分来源，可选）
        msg_time:    消息时间（可选，缺省用当前时间）
    """
    if not _text_msg_notifier:
        return

    title = f"💬 {room_name}"
    lines = [
        "### 💬 群消息",
        f"- **群**: {room_name}",
        f"- **发送人**: {sender_name or '未知'}",
        f"- **时间**: {msg_time or time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **内容**: {text[:1000]}",
    ]
    if enterprise:
        lines.insert(1, f"- **企业**: {enterprise}")
    content = "\n".join(lines)

    threading.Thread(
        target=_text_msg_notifier.send, args=(title, content),
        kwargs={"dedup": False}, daemon=True
    ).start()


def notify_feedback(description: str, detail: str = "", webhook: str = "", secret: str = ""):
    """用户在经营报告页提交「反馈与建议」时通知钉钉群（复用新保司通知群 webhook）。"""
    if not webhook:
        return
    title = "❤ 反馈与建议"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### 💬 报告反馈与建议",
        f"- **时间**: {ts}",
    ]
    if detail:
        lines.append(f"- **来源**: {detail[:500]}")
    lines.append(f"- **内容**: {(description or '')[:1000]}")
    content = "\n".join(lines)
    notifier = DingTalkNotifier(webhook, secret)
    threading.Thread(target=notifier.send, args=(title, content), daemon=True).start()
