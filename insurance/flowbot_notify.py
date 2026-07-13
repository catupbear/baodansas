"""
FlowBot 群消息通知（识别失败时向监控群发送提示并 @ 发送人）。

走第三方 FlowBot（飞流机器人）HTTP 接口，按「群名称」定位群、按「微信名」@ 人。
前提：对应的 FlowBot 机器人必须已在目标微信群内，否则找不到群、发送失败。
本模块所有异常均自行吞掉（旁路通知，绝不影响保单识别主流程）。
"""

import logging

import requests

logger = logging.getLogger(__name__)

FLOWBOT_API = "https://flowbot.feiliu.run/api/sendTask"

# FlowBot 消息类型码
_TYPE_TEXT = 50009  # 文字（支持 atList @人）


def send_flowbot_group_message(group_name: str, wechat_name, message: str, robot_id: str) -> bool:
    """
    通过 FlowBot 向指定微信群发送一条文字消息（可 @ 一人或多人）。

    Args:
        group_name:  目标群名称（FlowBot 按名称模糊定位群）
        wechat_name: 要 @ 的成员微信名；单人传字符串，多人传列表（空/None 则不 @）
        message:     消息正文
        robot_id:    FlowBot 机器人 ID（台账专用，不同于报价系统）

    Returns:
        是否发送成功（失败只记日志、返回 False，不抛异常）
    """
    if not group_name or not robot_id:
        logger.warning("FlowBot 群通知缺少群名或 robotId，跳过（group=%r robot=%r）",
                       group_name, robot_id)
        return False

    if isinstance(wechat_name, (list, tuple)):
        at_list = [n for n in wechat_name if n]
    else:
        at_list = [wechat_name] if wechat_name else []
    task_list = [{
        "type": _TYPE_TEXT,
        "atList": at_list,
        "searchText": group_name,
        "message": message,
    }]

    try:
        resp = requests.post(
            FLOWBOT_API,
            params={"robotId": robot_id},
            json={"taskList": task_list},
            timeout=10,
        )
        result = resp.json()
        if result.get("code") == 200:
            logger.info("✅ 已发送群通知到【%s】@%s: %s",
                        group_name, "、".join(at_list) or "-", message)
            return True
        logger.warning("⚠ 识别失败群通知发送失败: %s", result)
        return False
    except Exception as e:
        logger.warning("⚠ 识别失败群通知异常（不影响主流程）: %s", e)
        return False
