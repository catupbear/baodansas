#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保单识别处理器
负责：消息队列消费、媒体文件下载、OCR 识别、字段映射、COS 存储、钉钉同步
"""

import gc
import json
import logging
import os
import queue
import tempfile
import threading
import time
from typing import Optional

import hashlib
import pymysql

from insurance.db import (
    check_file_md5_exists,
    get_insurance_config,
    get_insurance_record,
    save_insurance_record,
    set_insurance_config,
    update_insurance_record,
)
from insurance.monitor_config_db import get_enabled_monitor_configs
from insurance.field_mapping import apply_mapping
from insurance.ocr_service import extract_text_from_pdf_bytes
from insurance.policy_parser import parse_policy_text, parse_policy_text_multi, _find_policy_boundaries
from core.notify import notify_error, notify_new_company

logger = logging.getLogger(__name__)

# 超过该大小（20MB）的文件走临时文件下载
MAX_MEMORY_SIZE = 20 * 1024 * 1024

# 保单识别队列容量上限：单日峰值约 700 份，1000 留足突发缓冲，满载才丢弃新消息
QUEUE_MAXSIZE = 1000


def _ocr_early_stop_check(combined_text: str) -> bool:
    """检查已扫描文本是否已包含所有导出所需字段，用于提前终止 OCR 扫描。
    导出字段：保险公司、保单号、险种类型、车牌号、投保人、被保险人、
             签单日期、保险起期、保险止期、保费合计、车船税、业务员。
    仅当文本中只包含单份保单时才提前停止，多保单PDF需扫描完整。
    """
    try:
        result = parse_policy_text(combined_text)
        fields = result.get("fields", {})
        import re
        # 人保财险多保单PDF常见（交强+商业+驾乘），直接扫全部页不早停
        company = fields.get("保险公司", "")
        if "人民财产" in company or re.search(r'PICC.*中国人民保险|中国人民财产保险', combined_text):
            return False
        # 通用多保单检测：检查是否存在多个保单标题或多个不同保单号
        boundaries = _find_policy_boundaries(combined_text)
        if len(boundaries) > 1:
            return False
        # 兜底：即使标题匹配不足，也检查是否存在多个不同保单号
        policy_no_pattern = r"保[险]?单号码?[：:\s]*([A-Za-z0-9]{10,30})"
        seen_nos = []
        for m in re.finditer(policy_no_pattern, combined_text):
            no_val = m.group(1)
            is_dup = any(no_val.startswith(sn) or sn.startswith(no_val) for sn in seen_nos)
            if not is_dup:
                seen_nos.append(no_val)
        if len(seen_nos) > 1:
            return False
        # 所有导出所需字段全部提取到才提前停止
        required_fields = [
            "保险公司", "保单号", "险种类型", "车牌号",
            "投保人", "被保险人", "签单日期",
            "保险起期", "保险止期", "保费合计",
            "业务员",
        ]
        # 车船税仅交强险需要，非交强险不作为必要字段
        policy_type = fields.get("险种类型", "")
        if "交强" in policy_type:
            required_fields.append("车船税")
        return all(f in fields for f in required_fields)
    except Exception:
        return False


def maybe_notify_new_company(db, parsed_fields, doc_category, company_short, filename, record_id):
    """识别为保单但归不出保司简称 → 钉钉通知"新保司"（持久去重）。
    自动识别与手动重新识别共用，确保两条路径都会提醒。
    全称缺失时用「保司公司名称」兜底，避免不同新保司互相去重漏报。
    """
    if doc_category != "保单" or company_short:
        return
    try:
        cf = (parsed_fields.get("保险公司") or parsed_fields.get("保司公司名称") or "").strip()
        if cf:
            key = cf
        else:
            # 全称也缺失时，用保单号前缀区分不同保司：否则所有归不出全称的保单
            # 都退化成同一个"未识别保司" key，第一份提醒后其余全被去重漏报。
            _pno = (parsed_fields.get("保单号") or "").strip()
            key = ("未识别保司#" + _pno[:9]) if _pno else "未识别保司"
        notified = get_insurance_config(db, "notified_new_companies", [])
        if not isinstance(notified, list):
            notified = []
        if key in notified:
            return
        notified.append(key)
        set_insurance_config(db, "notified_new_companies", notified)
        plate = parsed_fields.get("车牌号", "") or parsed_fields.get("车牌", "")
        ntf = get_insurance_config(db, "new_company_notify", {}) or {}
        notify_new_company(cf, f"文件: {filename} | record_id={record_id} | 车牌: {plate}",
                           ntf.get("webhook", ""), ntf.get("secret", ""))
        logger.info("发现新保司，已通知: %s (record_id=%s)", key, record_id)
    except Exception:
        logger.debug("新保司通知失败", exc_info=True)


class InsuranceHandler:
    """保单识别处理器（异步队列 + 守护线程）"""

    def __init__(
        self,
        db,
        finance_sdk=None,
        sdk_config=None,
        cos_storage=None,
        contacts=None,
        app_config=None,
    ):
        """
        初始化处理器。

        Args:
            db:          storage.db.Database 实例（有 db.pool 连接池）
            finance_sdk: 企业微信 SDK（可为 None）
            sdk_config:  dict，包含 proxy、proxy_passwd、timeout
            cos_storage: COS 存储客户端（可为 None）
            contacts:    通讯录客户端（可为 None）
            app_config:  dict，完整的 config.yaml 配置（含 baidu_ocr、dingtalk 等）
        """
        self.db = db
        self.finance_sdk = finance_sdk
        self.sdk_config = sdk_config or {}
        self.cos_storage = cos_storage
        self.contacts = contacts
        self.app_config = app_config or {}

        # 内部消息队列（最多 QUEUE_MAXSIZE 条）
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

        # OCR 客户端初始化
        self._baidu_ocr = None
        self._volc_ocr = None
        self._init_baidu_ocr()
        self._init_volc_ocr()

        # 火山引擎 OCR 熔断：连续失败 N 次后自动跳过，一段时间后恢复尝试
        self._volc_fail_count = 0
        self._volc_fail_threshold = 1  # 失败 1 次即熔断，快速切换百度
        self._volc_circuit_open_until = 0  # 熔断恢复时间戳（time.time()）
        self._volc_circuit_open_times = []  # 近期熔断发生的时间戳，用于告警降噪（频繁熔断才告警）

        # 启动消费者守护线程
        self._worker = threading.Thread(target=self._consume, daemon=True)
        self._worker.start()
        logger.info("InsuranceHandler 初始化完成，消费线程已启动")

    # ------------------------------------------------------------------ #
    # 百度 OCR 初始化
    # ------------------------------------------------------------------ #

    def _init_baidu_ocr(self):
        """初始化百度 OCR 客户端（优先从 config.yaml，其次从数据库）。"""
        try:
            from insurance.baidu_ocr import BaiduOCR

            # 每次初始化都重新读 config.yaml，支持热加载
            cfg = {}
            try:
                import yaml
                with open("config.yaml", "r", encoding="utf-8") as f:
                    file_cfg = yaml.safe_load(f) or {}
                cfg = file_cfg.get("baidu_ocr", {})
                # 同步更新内存中的 app_config
                if cfg:
                    self.app_config["baidu_ocr"] = cfg
            except Exception:
                cfg = self.app_config.get("baidu_ocr", {})
            if not cfg or not cfg.get("api_key"):
                # 降级从数据库读取
                cfg = get_insurance_config(self.db, "baidu_ocr", {})
            api_key = cfg.get("api_key", "") if isinstance(cfg, dict) else ""
            secret_key = cfg.get("secret_key", "") if isinstance(cfg, dict) else ""

            if api_key and secret_key:
                accuracy = cfg.get("accuracy", "accurate") if isinstance(cfg, dict) else "accurate"
                self._baidu_ocr = BaiduOCR(api_key, secret_key, accuracy=accuracy)
                accuracy_label = {"accurate": "高精度版", "standard": "标准版"}.get(accuracy, accuracy)
                logger.info("百度 OCR 客户端初始化成功（精度: %s）", accuracy_label)
            else:
                self._baidu_ocr = None
                logger.warning("百度 OCR 未配置 api_key / secret_key，降级功能不可用")
        except Exception as e:
            self._baidu_ocr = None
            logger.error("百度 OCR 初始化失败: %s", e)

    def reload_baidu_ocr(self):
        """公开方法：重新从配置文件读取并重建百度 OCR 客户端。"""
        logger.info("重新加载百度 OCR 配置")
        self._init_baidu_ocr()

    # ------------------------------------------------------------------ #
    # 火山引擎 OCR 初始化
    # ------------------------------------------------------------------ #

    def _init_volc_ocr(self):
        """初始化火山引擎 OCR 客户端（从 config.yaml 读取）。"""
        try:
            from insurance.volc_ocr import VolcOCR

            cfg = {}
            try:
                import yaml
                with open("config.yaml", "r", encoding="utf-8") as f:
                    file_cfg = yaml.safe_load(f) or {}
                cfg = file_cfg.get("volc_ocr", {})
                if cfg:
                    self.app_config["volc_ocr"] = cfg
            except Exception:
                cfg = self.app_config.get("volc_ocr", {})
            if not cfg or not cfg.get("access_key_id"):
                cfg = get_insurance_config(self.db, "volc_ocr", {})

            ak = cfg.get("access_key_id", "") if isinstance(cfg, dict) else ""
            sk = cfg.get("secret_access_key", "") if isinstance(cfg, dict) else ""

            if ak and sk:
                self._volc_ocr = VolcOCR(ak, sk)
                logger.info("火山引擎 OCR 客户端初始化成功")
            else:
                self._volc_ocr = None
                logger.info("火山引擎 OCR 未配置，跳过初始化")
        except Exception as e:
            self._volc_ocr = None
            logger.error("火山引擎 OCR 初始化失败: %s", e)

    def reload_volc_ocr(self):
        """公开方法：重新加载火山引擎 OCR 配置。"""
        logger.info("重新加载火山引擎 OCR 配置")
        self._init_volc_ocr()

    def reload_all_ocr(self):
        """重新加载所有 OCR 引擎配置。"""
        self.reload_baidu_ocr()
        self.reload_volc_ocr()

    # ------------------------------------------------------------------ #
    # 监控群列表
    # ------------------------------------------------------------------ #

    def get_watch_config(self) -> dict:
        """
        从 monitor_configs 表获取所有启用的监控来源。

        返回: {"rooms": [roomid, ...], "users": [userid, ...]}
        兼容旧逻辑：所有监控配置的来源合并为一个扁平列表。
        """
        try:
            configs = get_enabled_monitor_configs(self.db)
            rooms = set()
            users = set()
            for cfg in configs:
                for r in (cfg.get("rooms") or []):
                    if isinstance(r, dict) and r.get("id"):
                        rooms.add(r["id"])
                    elif isinstance(r, str) and r:
                        rooms.add(r)
                for u in (cfg.get("users") or []):
                    if isinstance(u, dict) and u.get("id"):
                        users.add(u["id"])
                    elif isinstance(u, str) and u:
                        users.add(u)
            return {"rooms": list(rooms), "users": list(users)}
        except Exception as e:
            logger.error("获取监控列表失败: %s", e)
            return {"rooms": [], "users": []}

    def get_watch_rooms(self) -> list:
        """兼容方法，返回监控群 ID 列表"""
        return self.get_watch_config().get("rooms", [])

    def _get_matched_monitors(self, roomid: str, sender: str) -> list:
        """
        根据 roomid / sender 查找匹配的监控配置。

        返回匹配的监控配置列表（包含 dingtalk_base_id, dingtalk_sheet_id, field_mapping 等）。
        一条消息可能匹配多个监控配置，各自独立同步。
        """
        try:
            configs = get_enabled_monitor_configs(self.db)
        except Exception as e:
            logger.error("获取监控配置失败: %s", e)
            return []

        matched = []
        for cfg in configs:
            room_ids = {
                r["id"] if isinstance(r, dict) else r
                for r in (cfg.get("rooms") or [])
                if (isinstance(r, dict) and r.get("id")) or (isinstance(r, str) and r)
            }
            user_ids = {
                u["id"] if isinstance(u, dict) else u
                for u in (cfg.get("users") or [])
                if (isinstance(u, dict) and u.get("id")) or (isinstance(u, str) and u)
            }

            if roomid and roomid in room_ids:
                matched.append(cfg)
            elif not roomid and sender and sender in user_ids:
                matched.append(cfg)

        return matched

    def _notify_fail_to_group(self, roomid: str, room_name: str, sender: str,
                              sender_name: str, filename: str):
        """
        保单识别失败时，通过 FlowBot 向监控群发送提示并 @ 发送人。

        仅在「监控群」内启用，且需在配置 flowbot_fail_notify 中开启并填入 robot_id。
        旁路通知：任何异常只记日志，绝不影响识别主流程。
        """
        try:
            cfg = get_insurance_config(self.db, "flowbot_fail_notify", {}) or {}
            if not cfg.get("enabled") or not cfg.get("robot_id"):
                return
            if not room_name:
                logger.info("识别失败群通知跳过：无群名 roomid=%s filename=%s", roomid, filename)
                return
            # 仅监控群启用：命中监控配置才通知
            if not self._get_matched_monitors(roomid, sender):
                return
            from insurance.flowbot_notify import send_flowbot_group_message
            msg = (cfg.get("template")
                   or "⚠️ 文档识别异常\n文件：{filename}\n请检查文档是否清晰完整后重新发送")
            msg = msg.replace("{filename}", filename or "未知文件")
            send_flowbot_group_message(room_name, sender_name, msg, cfg["robot_id"])
        except Exception as e:
            logger.warning("识别失败群通知发送失败（不影响主流程）: %s", e)

    # ------------------------------------------------------------------ #
    # 队列投递
    # ------------------------------------------------------------------ #

    def enqueue(self, msg: dict):
        """
        非阻塞投入消息队列。

        Args:
            msg: 包含 seq, roomid, sender, filename, sdkfileid, filesize 的字典
        """
        try:
            self._queue.put_nowait(msg)
            logger.debug(
                "消息已入队, seq=%s, filename=%s, 队列长度=%d",
                msg.get("seq"),
                msg.get("filename"),
                self._queue.qsize(),
            )
        except queue.Full:
            logger.warning(
                "保单识别队列已满（maxsize=%d），丢弃消息 seq=%s filename=%s",
                QUEUE_MAXSIZE,
                msg.get("seq"),
                msg.get("filename"),
            )

    # ------------------------------------------------------------------ #
    # 消费线程
    # ------------------------------------------------------------------ #

    def _consume(self):
        """守护线程主循环：持续从队列取消息并处理。"""
        logger.info("保单识别消费线程启动")
        while True:
            try:
                msg = self._queue.get(block=True, timeout=1)
            except queue.Empty:
                continue

            try:
                self._process(msg)
            except Exception as e:
                logger.error(
                    "保单处理异常, seq=%s filename=%s: %s",
                    msg.get("seq"),
                    msg.get("filename"),
                    e,
                    exc_info=True,
                )
                notify_error("保单识别", "_consume", str(e), f"seq={msg.get('seq')}, filename={msg.get('filename')}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # 单条消息处理主流程
    # ------------------------------------------------------------------ #

    def _process(self, msg: dict):
        """
        处理单个来自群聊的 PDF 消息。

        流程：解析发送人/群名 → 创建记录 → 下载 → 上传 COS → OCR → 更新记录 → 同步钉钉
        """
        seq = msg.get("seq")
        roomid = msg.get("roomid", "")
        sender = msg.get("sender", "")
        filename = msg.get("filename", "")
        sdkfileid = msg.get("sdkfileid", "")
        filesize = msg.get("filesize", 0)

        logger.info("开始处理保单 seq=%s filename=%s", seq, filename)

        # 1. 解析发送人/群名
        sender_name = ""
        room_name = ""
        if self.contacts:
            try:
                sender_name = self.contacts.get_name(sender) or ""
            except Exception as e:
                logger.warning("获取发送人姓名失败: %s", e)
        # 群名优先查统一群信息表（task_group_sync 独立任务维护，命中则无 API 调用）
        if roomid:
            try:
                from storage.group_db import get_group_name_map
                room_name = get_group_name_map(self.db, [roomid]).get(roomid, "")
            except Exception as e:
                logger.warning("查询统一群信息表失败: %s", e)
        if not room_name and self.contacts:
            try:
                room_name = self.contacts.get_room_name(roomid) or ""
            except Exception as e:
                logger.warning("获取群名失败: %s", e)

        # 2. 获取匹配的监控配置，提取绑定的 user_id 和 enterprise_id
        matched_monitors = self._get_matched_monitors(roomid, sender)
        config_user_id = None
        config_enterprise_id = None
        for m in matched_monitors:
            if m.get("user_id"):
                config_user_id = m["user_id"]
            if m.get("enterprise_id"):
                config_enterprise_id = m["enterprise_id"]
            if config_user_id:
                break

        # 按 sender 绑定关系查找用户（优先级高于监控配置）
        try:
            from auth.db import get_binding_by_sender
            bound = get_binding_by_sender(self.db, sender, config_enterprise_id)
            if bound:
                config_user_id = bound["user_id"]
                if not config_enterprise_id and bound.get("enterprise_id"):
                    config_enterprise_id = bound["enterprise_id"]
        except Exception as e:
            logger.warning("查找 sender 绑定用户失败: %s", e)

        # 3. 检查企业月度配额（标准版限 3000 次/月）
        if config_enterprise_id:
            try:
                from auth.db import check_enterprise_quota
                _quota = check_enterprise_quota(self.db, config_enterprise_id)
                if _quota.get("expired"):
                    logger.warning(
                        "企业 %s 套餐已到期，跳过 seq=%s filename=%s",
                        config_enterprise_id, seq, filename,
                    )
                    return
                if _quota.get("exceeded"):
                    logger.warning(
                        "企业 %s 本月识别额度已用完（%s/%s），跳过 seq=%s filename=%s",
                        config_enterprise_id, _quota.get("usage"), _quota.get("limit"), seq, filename,
                    )
                    return
            except Exception as e:
                logger.warning("配额检查失败，继续处理: %s", e)

        # 3.5 二次保险：万一有其他并发路径(如手动补扫接口和启动补扫同时触发)绕过了启动锁，
        # 这里再兜底查一次"这条消息是否已经有记录"，命中就直接跳过、不再重复创建
        # （2026-07-15，配合main.py启动补拉/补扫的命名锁，双重防止重复记录）
        try:
            _seq_conn = self.db.pool.connection()
            try:
                _seq_cursor = _seq_conn.cursor(pymysql.cursors.DictCursor)
                _seq_cursor.execute(
                    "SELECT id FROM insurance_records WHERE msg_seq = %s AND policy_index = 1 LIMIT 1",
                    (seq,),
                )
                _dup = _seq_cursor.fetchone()
            finally:
                _seq_conn.close()
            if _dup:
                logger.info("跳过重复处理: seq=%s 已存在记录 id=%s", seq, _dup["id"])
                return
        except Exception as e:
            logger.warning("重复消息检查失败，继续处理: %s", e)

        # 4. 创建初始记录（状态: processing）
        initial_record = {
            "msg_seq": seq,
            "roomid": roomid,
            "room_name": room_name,
            "sender": sender,
            "sender_name": sender_name,
            "filename": filename,
            "filesize": filesize,
            "status": "processing",
            "source": "auto",
            "user_id": config_user_id,
            "enterprise_id": config_enterprise_id,
        }
        try:
            record_id = save_insurance_record(self.db, initial_record)
        except Exception as e:
            logger.error("创建保单记录失败: %s", e)
            return

        pdf_bytes = b""
        cos_url = ""
        try:
            # 3. 下载媒体文件（优先使用消息携带的 SDK，支持多企业）
            msg_sdk = msg.get("finance_sdk")
            msg_sdk_config = msg.get("sdk_config")
            pdf_bytes = self._download_media(sdkfileid, filesize, msg_sdk, msg_sdk_config)
            if not pdf_bytes:
                raise RuntimeError("媒体文件下载失败或内容为空")

            # 3.1 计算文件 MD5 并去重
            file_md5 = hashlib.md5(pdf_bytes).hexdigest()
            update_insurance_record(self.db, record_id, {"file_md5": file_md5})

            existing = check_file_md5_exists(self.db, file_md5)
            if existing and existing["id"] != record_id:
                existing_full = get_insurance_record(self.db, existing["id"])
                # 用 msg_seq 区分"同一次发送被重复处理" vs "不同次发送(重发)"：
                #  - 同一条消息(seq 相同)被重复拉取/处理 → 标记 duplicate 跳过，避免同一次发送重复入库；
                #  - 不同次发送(seq 不同，含同一人不同时段重发、或不同人转发) → 复制识别结果。
                _same_msg = bool(existing_full and existing_full.get("msg_seq") and existing_full.get("msg_seq") == seq)
                if existing_full and not _same_msg:
                    _same_enterprise = (
                        existing_full.get("enterprise_id") is not None
                        and config_enterprise_id is not None
                        and existing_full.get("enterprise_id") == config_enterprise_id
                    )
                    logger.info(
                        "[COPY] PDF重复(不同次发送, MD5=%s), 原记录id=%d(seq=%s,sender=%s), 当前record_id=%d(seq=%s,sender=%s), 同企业=%s, 复制识别结果",
                        file_md5, existing["id"], existing_full.get("msg_seq"), existing_full.get("sender"),
                        record_id, seq, sender, _same_enterprise,
                    )
                    self._copy_recognition_result(record_id, existing_full, file_md5, config_user_id, config_enterprise_id)
                    return
                # 同一条消息重复处理，标记为 duplicate
                logger.info(
                    "PDF文件重复(同一次发送 seq=%s, MD5=%s), 已有记录id=%d, 当前record_id=%d, 跳过识别",
                    seq, file_md5, existing["id"], record_id,
                )
                update_insurance_record(self.db, record_id, {
                    "status": "duplicate",
                    "error_message": f"文件重复，与记录 #{existing['id']}({existing.get('filename', '')}) 相同",
                })
                return

            # 4. 校验文件格式（非 PDF 直接静默失败，不触发 OCR 熔断）
            if not pdf_bytes.startswith(b'%PDF') and not pdf_bytes.startswith(b'\x25\x50\x44\x46'):
                # 尝试识别实际格式给出友好提示
                if pdf_bytes[:5] in (b'<?xml', b'<html', b'<!DOC'):
                    fmt = 'HTML/XML'
                elif pdf_bytes[:4] == b'\x89PNG':
                    fmt = 'PNG'
                elif pdf_bytes[:3] == b'\xff\xd8\xff':
                    fmt = 'JPEG'
                else:
                    fmt = f'未知({pdf_bytes[:8].hex()})'
                err_msg = f'文件格式无效（实际为 {fmt}，非 PDF）'
                logger.warning("文件格式无效，静默跳过, record_id=%d filename=%s: %s", record_id, filename, err_msg)
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "error_message": err_msg,
                })
                return

            # 4b. 上传 COS
            cos_url = self._upload_to_cos(pdf_bytes, roomid, sender, filename)

            # 5. OCR 识别
            ocr_result = self._do_ocr(pdf_bytes, filename)
            if not ocr_result.get("success"):
                err_msg = ocr_result.get("error", "未知错误")
                logger.warning("OCR 未能提取文字，静默标记失败, record_id=%d filename=%s: %s", record_id, filename, err_msg)
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "error_message": err_msg[:500],
                    "cos_url": cos_url,
                })
                # 真识别失败：向监控群发送提示并 @ 发送人
                self._notify_fail_to_group(roomid, room_name, sender, sender_name, filename)
                return

            policies = ocr_result.get("policies", [])
            ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")
            ocr_raw_text = ocr_result.get("ocr_text", "")

            if len(policies) > 1:
                logger.info(
                    "多保单PDF: filename=%s, 共%d份保单, record_id=%d",
                    filename, len(policies), record_id,
                )

            # 9. 同步前二次解析发送人名称（首次可能因缓存未命中返回ID）
            if sender and sender_name == sender and self.contacts:
                try:
                    resolved = self.contacts.get_name(sender) or ""
                    if resolved and resolved != sender:
                        sender_name = resolved
                        logger.info("发送人名称二次解析成功: %s -> %s", sender, sender_name)
                except Exception as e:
                    logger.warning("发送人名称二次解析失败: %s", e)

            # 同一 PDF 多保单之间字段互补（车牌、投保人等基础字段）
            if len(policies) > 1:
                self._cross_fill_same_pdf(policies)

            # 6-9. 逐条处理每份保单（多保单PDF会产生多条记录）
            total_policies = len(policies)
            logger.info(
                "开始逐条处理多保单, 共%d份, record_id=%d, file_md5=%s",
                total_policies, record_id, file_md5,
            )
            for policy_idx, policy in enumerate(policies):
                cur_record_id = record_id  # 第一条复用已创建的记录
                policy_type = policy.get("fields", {}).get("险种类型", "未知")
                logger.info(
                    "处理第%d/%d份保单: 险种=%s, confidence=%.2f",
                    policy_idx + 1, total_policies, policy_type, policy.get("confidence", 0),
                )

                if policy_idx > 0:
                    # 多保单的第2条及之后，创建新记录
                    extra_record = {
                        "msg_seq": seq,
                        "roomid": roomid,
                        "room_name": room_name,
                        "sender": sender,
                        "sender_name": sender_name,
                        "filename": filename,
                        "filesize": filesize,
                        "status": "processing",
                        "source": "auto",
                        "user_id": config_user_id,
                        "enterprise_id": config_enterprise_id,
                        "file_md5": hashlib.md5(pdf_bytes).hexdigest() if pdf_bytes else None,
                        "cos_url": cos_url,
                        "policy_count": total_policies,
                        "policy_index": policy_idx + 1,
                    }
                    try:
                        cur_record_id = save_insurance_record(self.db, extra_record)
                        logger.info(
                            "多保单PDF第%d/%d条, 新建record_id=%d, 险种=%s",
                            policy_idx + 1, len(policies), cur_record_id, policy_type,
                        )
                    except Exception as e:
                        logger.error(
                            "多保单创建第%d/%d条记录失败(险种=%s, file_md5=%s): %s",
                            policy_idx + 1, len(policies), policy_type, file_md5, e,
                            exc_info=True,
                        )
                        continue

                parsed_fields = policy.get("fields", {})
                doc_category = policy.get("doc_category", "")
                confidence = policy.get("confidence", 0.0)

                # 文件名带"投保"时文档类型标记为投保单
                if filename and "投保" in filename:
                    doc_category = "投保单"
                    parsed_fields["文档类型"] = "投保单"

                # 6. 保单字段互补（车牌→车架号→人名，逐级回填无车牌记录）
                self._cross_fill_by_plate(parsed_fields, cur_record_id)
                self._cross_fill_by_vin(parsed_fields, cur_record_id)
                self._cross_fill_by_person(parsed_fields, cur_record_id)

                # 6b. TZ-008 专属：车牌为空时用车架号填入车牌号（持久化）
                from insurance.db import apply_tz008_plate_fallback
                apply_tz008_plate_fallback(self.db, parsed_fields, config_enterprise_id)

                # 7. 字段映射
                company_short = parsed_fields.get("保险公司简称", "")
                mapped_fields = apply_mapping(parsed_fields, company_short)

                # 8. 更新记录为 done
                raw_text = policy.get("raw_text", "")
                page_range = policy.get("page_range", "")[:255]
                updates = {
                    "status": "done",
                    "cos_url": cos_url,
                    "ocr_engine": ocr_engine,
                    "doc_category": doc_category,
                    "confidence": confidence,
                    "parsed_fields": parsed_fields,
                    "mapped_fields": mapped_fields,
                    "raw_text": raw_text,
                    "policy_count": total_policies,
                    "policy_index": policy_idx + 1,
                    "page_range": page_range,
                }
                if ocr_raw_text:
                    updates["ocr_text"] = ocr_raw_text
                update_insurance_record(self.db, cur_record_id, updates)

                # 发现规则外的新保司 → 钉钉通知（自动识别与手动重新识别共用同一函数）
                maybe_notify_new_company(self.db, parsed_fields, doc_category, company_short, filename, cur_record_id)

                # 更新发送人名称
                if sender_name and sender_name != sender:
                    update_insurance_record(self.db, cur_record_id, {"sender_name": sender_name})

                logger.info(
                    "保单处理完成, record_id=%d filename=%s (%d/%d)",
                    cur_record_id, filename, policy_idx + 1, len(policies),
                )

                # 9. 非保单类型不同步钉钉，只记录日志
                if doc_category and doc_category != "保单":
                    logger.info(
                        "文档类型为「%s」（非保单），跳过钉钉同步, record_id=%d filename=%s",
                        doc_category, cur_record_id, filename,
                    )
                else:
                    # 自动同步钉钉（按监控配置决定，复用之前匹配的监控列表）
                    try:
                        for monitor in matched_monitors:
                            if not (monitor.get("dingtalk_base_id") and monitor.get("dingtalk_sheet_id")):
                                continue
                            monitor_mapping = monitor.get("field_mapping") or {}
                            if monitor_mapping:
                                from insurance.field_mapping import DEFAULT_MAPPING
                                export_to_ocr = {v: k for k, v in DEFAULT_MAPPING.items() if v}
                                sync_fields = {}
                                for export_col, target_col in monitor_mapping.items():
                                    if not target_col:
                                        continue
                                    ocr_field = export_to_ocr.get(export_col, export_col)
                                    if ocr_field in parsed_fields:
                                        sync_fields[target_col] = parsed_fields[ocr_field]
                                    elif export_col in parsed_fields:
                                        sync_fields[target_col] = parsed_fields[export_col]
                            else:
                                sync_fields = mapped_fields

                            self._sync_to_dingtalk_v2(
                                cur_record_id, sync_fields,
                                sender_name=sender_name,
                                cos_url=cos_url,
                                doc_category=doc_category,
                                monitor=monitor,
                            )
                    except Exception as e:
                        logger.error("自动同步钉钉失败, record_id=%d: %s", cur_record_id, e)
                        notify_error("钉钉同步", "_sync_to_dingtalk_v2", str(e), f"record_id={cur_record_id}")

        except Exception as e:
            logger.error("保单处理失败, record_id=%d filename=%s: %s", record_id, filename, e, exc_info=True)
            notify_error("保单识别", "_process", str(e), f"record_id={record_id}, filename={filename}")
            try:
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "error_message": str(e)[:500],
                    "cos_url": cos_url,
                })
            except Exception as ue:
                logger.error("更新失败状态时出错: %s", ue)
            # 真识别失败（处理异常）：向监控群发送提示并 @ 发送人
            self._notify_fail_to_group(roomid, room_name, sender, sender_name, filename)
        finally:
            # 释放内存
            del pdf_bytes
            gc.collect()

    # ------------------------------------------------------------------ #
    # 媒体下载
    # ------------------------------------------------------------------ #

    def _download_media(self, sdkfileid: str, filesize: int,
                        msg_sdk=None, msg_sdk_config: dict = None) -> bytes:
        """
        从企业微信 SDK 下载媒体文件。

        - 优先使用消息携带的 SDK（支持多企业）
        - filesize > MAX_MEMORY_SIZE 时走临时文件
        - 否则直接下载到内存

        Args:
            sdkfileid:      SDK 文件 ID
            filesize:       文件大小（字节）
            msg_sdk:        消息来源企业的 SDK 实例（优先使用）
            msg_sdk_config: 消息来源企业的 SDK 配置

        Returns:
            文件字节内容，失败返回 b""
        """
        sdk = msg_sdk or self.finance_sdk
        sdk_conf = msg_sdk_config or self.sdk_config
        sdk_source = "消息携带" if msg_sdk else "默认"
        if not sdk:
            logger.warning("finance_sdk 未初始化，无法下载媒体文件")
            return b""
        logger.info("下载媒体文件使用 %s SDK (id=%s)", sdk_source, id(sdk))

        proxy = sdk_conf.get("proxy", "")
        passwd = sdk_conf.get("proxy_passwd", "")
        timeout = sdk_conf.get("timeout", 30)

        # 单次下载尝试：成功返回 (True, bytes)，失败返回 (False, ret 错误码)
        def _try_once():
            if filesize > MAX_MEMORY_SIZE:
                # 大文件走临时文件，避免占用大量内存
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
                os.close(tmp_fd)
                try:
                    ret, _ = sdk.get_media_data(
                        sdkfileid, proxy, passwd, timeout, tmp_path
                    )
                    if ret != 0:
                        return False, ret
                    with open(tmp_path, "rb") as f:
                        return True, f.read()
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            else:
                # 小文件直接下载到内存
                ret, data = sdk.get_media_data_bytes(
                    sdkfileid, proxy, passwd, timeout
                )
                if ret != 0:
                    return False, ret
                return True, (data if data else b"")

        # 下载偶发网络错误（如 10001）时自动重试，间隔递增 1s/2s
        max_attempts = 3
        last_ret = None
        for attempt in range(1, max_attempts + 1):
            try:
                ok, payload = _try_once()
                if ok:
                    return payload
                last_ret = payload
                logger.warning(
                    "下载媒体文件失败, ret=%s (第 %d/%d 次)",
                    last_ret, attempt, max_attempts,
                )
            except Exception as e:
                last_ret = str(e)
                logger.warning(
                    "下载媒体文件异常, sdkfileid=%s: %s (第 %d/%d 次)",
                    sdkfileid, e, attempt, max_attempts,
                    exc_info=(attempt == max_attempts),
                )
            if attempt < max_attempts:
                time.sleep(attempt)  # 1s, 2s 退避

        logger.error("下载媒体文件失败（已重试 %d 次）, ret=%s", max_attempts, last_ret)
        notify_error(
            "保单识别", "_download_media",
            f"下载媒体文件失败（已重试 {max_attempts} 次）, ret={last_ret}",
            f"sdkfileid={sdkfileid[:30]}...",
        )
        return b""

    # ------------------------------------------------------------------ #
    # 同一 PDF 多保单互补
    # ------------------------------------------------------------------ #

    # 同一 PDF 内多保单可互补的基础字段
    SAME_PDF_FILL_FIELDS = [
        "车牌号", "投保人", "被保险人", "车主", "证件号码",
        "车架号VIN", "发动机号", "厂牌型号",
        # 车辆描述字段：同一PDF内驾乘/意外险与车险共车，补齐使意外险车辆信息完整
        "使用性质", "机动车种类", "核定载客", "司机乘客", "车辆性质",
        "被保险人身份证号码", "投保人身份证号码",
        "保司公司名称", "保司地址",
    ]

    def _cross_fill_same_pdf(self, policies: list):
        """
        同一 PDF 内多份保单之间互补基础字段。
        例如：第1份保单有车牌号和投保人，第2份保单缺失这些字段时自动补充。
        """
        # 收集所有保单中已有的字段值
        # 人名类字段取最长值（短值可能是OCR误提取），其他字段取第一个非空值
        from insurance.policy_parser import _is_valid_person
        PERSON_FIELDS = {"投保人", "被保险人", "车主"}
        merged = {}
        for policy in policies:
            fields = policy.get("fields", {})
            for f in self.SAME_PDF_FILL_FIELDS:
                val = fields.get(f, "")
                if not val:
                    continue
                if f in PERSON_FIELDS:
                    # 人名字段：取最长的值（更可信），但必须通过验证
                    if _is_valid_person(val) and (f not in merged or len(val) > len(merged[f])):
                        merged[f] = val
                elif f not in merged:
                    merged[f] = val

        if not merged:
            return

        # 回填到每份保单的缺失或较短的人名字段
        for policy in policies:
            fields = policy.get("fields", {})
            filled = []
            for f, val in merged.items():
                cur = fields.get(f, "")
                if not cur:
                    fields[f] = val
                    filled.append(f"{f}={val}")
                elif f in PERSON_FIELDS and cur != val and len(val) > len(cur):
                    fields[f] = val
                    filled.append(f"{f}={val}(校验替换:{cur})")
            # 交强险保司校验：交强险是平安时，从同PDF其他保单取保司
            policy_type = fields.get("险种类型", "")
            cur_company = fields.get("保险公司", "")
            if "交通事故责任强制" in policy_type and "平安" in cur_company:
                for other in policies:
                    other_fields = other.get("fields", {})
                    if other_fields is fields:
                        continue
                    other_company = other_fields.get("保险公司", "")
                    if other_company and "平安" not in other_company:
                        fields["保险公司"] = other_company
                        fields["保险公司简称"] = other_fields.get("保险公司简称", "")
                        filled.append(f"保险公司={other_company}(交强险校验替换:{cur_company})")
                        break

            if filled:
                logger.info(
                    "同PDF互补: 险种=%s, 补充=%s",
                    fields.get("险种类型", "未知"),
                    ", ".join(filled),
                )

    # ------------------------------------------------------------------ #
    # 同车牌保单字段互补
    # ------------------------------------------------------------------ #

    # 可从同车牌历史保单互补的字段
    # 车辆描述字段(厂牌型号/发动机号/使用性质等)：电子标志/驾乘险自身无车辆信息时，从同车牌车险补齐
    CROSS_FILL_FIELDS = ["投保人", "被保险人", "车主", "车架号VIN", "被保险人身份证号码", "投保人身份证号码", "保司公司名称", "保司地址", "签单日期",
                         "厂牌型号", "发动机号", "使用性质", "机动车种类", "核定载客", "司机乘客", "车辆性质"]
    # 其中需要人名验证的字段
    _PERSON_VALIDATE_FIELDS = {"投保人", "被保险人", "车主"}

    @staticmethod
    def _is_vehicle_policy(parsed_fields: dict) -> bool:
        """判断是否为车险（交强险/商业险）"""
        policy_type = parsed_fields.get("险种类型", "")
        if "交通事故责任强制" in policy_type:
            return True
        if "商业保险" in policy_type or "机动车辆保险" in policy_type or "机动车辆综合险" in policy_type:
            return True
        return False

    @staticmethod
    def _is_non_vehicle_policy(parsed_fields: dict) -> bool:
        """判断是否为非车险（驾乘/意外/非车）"""
        policy_type = parsed_fields.get("险种类型", "")
        if not policy_type:
            return False
        from insurance.policy_parser import _get_policy_type_code
        type_code, _ = _get_policy_type_code(policy_type)
        return type_code in ("accident", "non_vehicle")

    def _cross_fill_by_plate(self, parsed_fields: dict, record_id: int = None):
        """同车牌保单双向字段互补（核心逻辑见 _apply_cross_fill）。"""
        plate = parsed_fields.get("车牌号", "")
        if not plate:
            return
        from insurance.db import find_records_by_plate
        history = find_records_by_plate(self.db, plate, exclude_id=record_id)
        if not history:
            return
        # 同车牌但车架号不同 = 不同车辆（套牌/换牌重号），跳过互补，避免张冠李戴
        # （如粤BNN599 既是全球鹰的宝骏，又是华诺旅游的海格客车，投保人/车主完全不同）
        cur_vin = (parsed_fields.get("车架号VIN", "") or "").strip()
        if len(cur_vin) >= 10:
            _filtered = []
            for rec in history:
                hf = rec.get("parsed_fields") or {}
                if isinstance(hf, str):
                    try:
                        hf = json.loads(hf) or {}
                    except (TypeError, json.JSONDecodeError, ValueError):
                        hf = {}
                hv = (hf.get("车架号VIN", "") or "").strip()
                if len(hv) >= 10 and hv != cur_vin:
                    continue  # 车架号明确不同，判为不同车辆
                _filtered.append(rec)
            history = _filtered
            if not history:
                return
        self._apply_cross_fill(parsed_fields, history, record_id,
                               self.CROSS_FILL_FIELDS, "同车牌", plate)

    # 可从同车架号历史保单互补的字段（用车架号匹配，故补车牌号，不补车架号本身）
    VIN_CROSS_FILL_FIELDS = ["车牌号", "投保人", "被保险人", "车主",
                             "被保险人身份证号码", "投保人身份证号码", "保司公司名称", "保司地址", "签单日期",
                             "厂牌型号", "发动机号", "使用性质", "机动车种类", "核定载客", "司机乘客", "车辆性质"]

    def _cross_fill_by_vin(self, parsed_fields: dict, record_id: int = None):
        """同车架号(VIN)保单双向互补：适用于"暂未上牌"等无车牌保单与同车车险互补。"""
        vin = (parsed_fields.get("车架号VIN", "") or "").strip()
        if len(vin) < 10:
            return
        from insurance.db import find_records_by_vin
        history = find_records_by_vin(self.db, vin, exclude_id=record_id)
        if not history:
            return
        self._apply_cross_fill(parsed_fields, history, record_id,
                               self.VIN_CROSS_FILL_FIELDS, "同车架号", vin)

    def _apply_cross_fill(self, parsed_fields: dict, history: list, record_id,
                          fill_fields: list, match_desc: str, match_value: str):
        """
        双向字段互补核心逻辑（车牌/车架号共用）：
        1. 正向：从历史记录补充当前保单的缺失字段
        2. 非车险被保人覆盖：非车险的被保人以交强险/商业险为准
        3. 反向：用当前保单的字段回填历史记录的缺失字段
        仅补充空值字段，不覆盖已有值（非车险被保人除外）。
        """
        from insurance.db import update_insurance_record
        from insurance.field_mapping import apply_mapping
        from insurance.policy_parser import _is_valid_person

        # --- 正向互补：历史 → 当前 ---
        missing = [f for f in fill_fields if not parsed_fields.get(f)]
        filled = []
        for field in missing:
            need_person_check = field in self._PERSON_VALIDATE_FIELDS
            for rec in history:
                hist_fields = rec.get("parsed_fields") or {}
                if isinstance(hist_fields, str):
                    try:
                        hist_fields = json.loads(hist_fields) or {}
                    except (TypeError, json.JSONDecodeError, ValueError):
                        hist_fields = {}
                val = hist_fields.get(field, "")
                if val and (not need_person_check or _is_valid_person(val)):
                    parsed_fields[field] = val
                    filled.append(f"{field}={val}")
                    break

        # 车险交叉校验：同车牌的交强险与商业险，投保人/被保险人/车主应一致
        # 当值不同时取更长的（短值往往是OCR从条款文本误提取的片段）
        if self._is_vehicle_policy(parsed_fields):
            for rec in history:
                hist_fields = rec.get("parsed_fields") or {}
                if isinstance(hist_fields, str):
                    try:
                        hist_fields = json.loads(hist_fields) or {}
                    except (TypeError, json.JSONDecodeError, ValueError):
                        hist_fields = {}
                if not self._is_vehicle_policy(hist_fields):
                    continue
                # 仅对人名字段做长度校验替换，身份证/车架号等非人名字段不做此替换
                for field in self._PERSON_VALIDATE_FIELDS:
                    cur_val = parsed_fields.get(field, "")
                    hist_val = hist_fields.get(field, "")
                    if cur_val and hist_val and cur_val != hist_val:
                        if len(hist_val) > len(cur_val) and _is_valid_person(hist_val):
                            parsed_fields[field] = hist_val
                            filled.append(f"{field}={hist_val}(校验替换:{cur_val})")
                break  # 只对比一条同类车险

        # 交强险保司校验：交强险识别为平安时，从同车牌其他保单取保司
        policy_type = parsed_fields.get("险种类型", "")
        cur_company = parsed_fields.get("保险公司", "")
        if "交通事故责任强制" in policy_type and "平安" in cur_company:
            for rec in history:
                hist_fields = rec.get("parsed_fields") or {}
                if isinstance(hist_fields, str):
                    try:
                        hist_fields = json.loads(hist_fields) or {}
                    except (TypeError, json.JSONDecodeError, ValueError):
                        hist_fields = {}
                hist_company = hist_fields.get("保险公司", "")
                hist_short = hist_fields.get("保险公司简称", "")
                if hist_company and "平安" not in hist_company:
                    parsed_fields["保险公司"] = hist_company
                    parsed_fields["保险公司简称"] = hist_short
                    filled.append(f"保险公司={hist_company}(交强险校验替换:{cur_company})")
                    break

        # 非车险覆盖：从交强险/商业险取投保人、被保人、车主，覆盖非车险对应字段
        if self._is_non_vehicle_policy(parsed_fields):
            override_fields = ["投保人", "被保险人", "车主"]
            for rec in history:
                hist_fields = rec.get("parsed_fields") or {}
                if isinstance(hist_fields, str):
                    try:
                        hist_fields = json.loads(hist_fields) or {}
                    except (TypeError, json.JSONDecodeError, ValueError):
                        hist_fields = {}
                if self._is_vehicle_policy(hist_fields):
                    for field in override_fields:
                        vehicle_val = hist_fields.get(field, "")
                        if vehicle_val and vehicle_val != parsed_fields.get(field, "") and _is_valid_person(vehicle_val):
                            old_val = parsed_fields.get(field, "")
                            parsed_fields[field] = vehicle_val
                            filled.append(f"{field}={vehicle_val}(覆盖:{old_val})")
                    break

        if filled:
            logger.info(
                "%s互补(正向): %s, record_id=%s, 补充=%s",
                match_desc, match_value, record_id, ", ".join(filled),
            )

        # --- 反向互补：当前 → 历史缺失记录 ---
        current_vals = {f: parsed_fields.get(f, "") for f in fill_fields}
        # 当前记录至少要有一个可供回填的字段
        if not any(current_vals.values()):
            return

        is_current_vehicle = self._is_vehicle_policy(parsed_fields)

        for rec in history:
            hist_fields = rec.get("parsed_fields", {})
            if not hist_fields:
                continue
            back_filled = []
            for field in fill_fields:
                need_person_check = field in self._PERSON_VALIDATE_FIELDS
                if not hist_fields.get(field) and current_vals.get(field):
                    if not need_person_check or _is_valid_person(current_vals[field]):
                        hist_fields[field] = current_vals[field]
                        back_filled.append(f"{field}={current_vals[field]}")

            # 反向校验：当前是车险，历史也是车险，人名字段短值被长值替换
            if is_current_vehicle and self._is_vehicle_policy(hist_fields):
                for field in self._PERSON_VALIDATE_FIELDS:
                    cur_val = current_vals.get(field, "")
                    hist_val = hist_fields.get(field, "")
                    if cur_val and hist_val and cur_val != hist_val and len(cur_val) > len(hist_val) and _is_valid_person(cur_val):
                        old_val = hist_val
                        hist_fields[field] = cur_val
                        back_filled.append(f"{field}={cur_val}(校验替换:{old_val})")

            # 反向覆盖：当前是车险，历史是非车险，覆盖历史非车险的投保人、被保人、车主
            if is_current_vehicle and self._is_non_vehicle_policy(hist_fields):
                for field in self._PERSON_VALIDATE_FIELDS:
                    cur_val = current_vals.get(field, "")
                    hist_val = hist_fields.get(field, "")
                    if cur_val and hist_val != cur_val and _is_valid_person(cur_val):
                        old_val = hist_val
                        hist_fields[field] = cur_val
                        back_filled.append(f"{field}={cur_val}(覆盖:{old_val})")

            if back_filled:
                # 重新计算 mapped_fields
                company_short = hist_fields.get("保险公司简称", "")
                mapped = apply_mapping(hist_fields, company_short)
                try:
                    update_insurance_record(self.db, rec["id"], {
                        "parsed_fields": hist_fields,
                        "mapped_fields": mapped,
                    })
                    logger.info(
                        "%s互补(反向): %s, 回填record_id=%s, 补充=%s",
                        match_desc, match_value, rec["id"], ", ".join(back_filled),
                    )
                    # 如果历史记录已同步过钉钉，重新同步更新的字段
                    self._resync_record_to_dingtalk(rec["id"], mapped, hist_fields)
                except Exception as e:
                    logger.warning("%s反向互补失败: record_id=%s, %s", match_desc, rec["id"], e)

    # 无车牌时按人名互补的字段
    # 注意：车架号VIN 不能放进来——它是"一车一码"，同一投保人/企业名下可能有多台不同的车，
    # 按人名匹配互补车架号会张冠李戴（如同一物流公司多台货车互相顶替车架号），
    # 车架号只应按车牌(_cross_fill_by_plate)或车架号本身(_cross_fill_by_vin)匹配互补。
    PERSON_FILL_FIELDS = ["车牌号", "投保人", "被保险人", "车主", "被保险人身份证号码", "投保人身份证号码"]

    def _cross_fill_by_person(self, parsed_fields: dict, record_id: int = None):
        """
        无车牌保单按人名匹配互补：
        通过投保人/被保险人/车主查找其他保单，优先匹配车险（非 非车险/驾意险），
        从匹配到的保单补充缺失字段（车牌号、投保人、被保险人、车主）。
        """
        applicant = parsed_fields.get("投保人", "")
        insured = parsed_fields.get("被保险人", "")
        owner = parsed_fields.get("车主", "")
        if not applicant and not insured and not owner:
            return

        from insurance.db import find_records_by_person, update_insurance_record
        from insurance.field_mapping import apply_mapping
        history = find_records_by_person(self.db, applicant, insured, owner, exclude_id=record_id)
        if not history:
            return

        # 排序：车险优先（非车险/驾意险排后面）
        def sort_key(rec):
            hf = rec.get("parsed_fields", {})
            if self._is_non_vehicle_policy(hf):
                return 1  # 非车险排后面
            return 0  # 车险优先
        history.sort(key=sort_key)

        # 正向互补：历史 → 当前
        missing = [f for f in self.PERSON_FILL_FIELDS if not parsed_fields.get(f)]
        filled = []
        if missing:
            for field in missing:
                for rec in history:
                    hist_fields = rec.get("parsed_fields") or {}
                    if isinstance(hist_fields, str):
                        try:
                            hist_fields = json.loads(hist_fields) or {}
                        except (TypeError, json.JSONDecodeError, ValueError):
                            hist_fields = {}
                    val = hist_fields.get(field, "")
                    if val:
                        parsed_fields[field] = val
                        filled.append(f"{field}={val}(from record:{rec['id']})")
                        break

            if filled:
                logger.info(
                    "按人名互补(正向): applicant=%s, insured=%s, owner=%s, record_id=%s, 补充=%s",
                    applicant, insured, owner, record_id, ", ".join(filled),
                )

        # 反向互补：当前 → 历史缺失记录
        current_vals = {f: parsed_fields.get(f, "") for f in self.PERSON_FILL_FIELDS}
        if not any(current_vals.values()):
            return

        for rec in history:
            hist_fields = rec.get("parsed_fields", {})
            if not hist_fields:
                continue
            back_filled = []
            for field in self.PERSON_FILL_FIELDS:
                if not hist_fields.get(field) and current_vals.get(field):
                    hist_fields[field] = current_vals[field]
                    back_filled.append(f"{field}={current_vals[field]}")

            if back_filled:
                company_short = hist_fields.get("保险公司简称", "")
                mapped = apply_mapping(hist_fields, company_short)
                try:
                    update_insurance_record(self.db, rec["id"], {
                        "parsed_fields": hist_fields,
                        "mapped_fields": mapped,
                    })
                    logger.info(
                        "按人名互补(反向): 回填record_id=%s, 补充=%s",
                        rec["id"], ", ".join(back_filled),
                    )
                    self._resync_record_to_dingtalk(rec["id"], mapped, hist_fields)
                except Exception as e:
                    logger.warning("按人名反向互补失败: record_id=%s, %s", rec["id"], e)

    def _resync_record_to_dingtalk(self, record_id: int, mapped_fields: dict, parsed_fields: dict):
        """
        反向互补后重新同步历史记录到钉钉（更新已有行）。
        从数据库获取记录的 roomid/sender，匹配监控配置后同步。
        """
        try:
            from insurance.db import get_insurance_record
            record = get_insurance_record(self.db, record_id)
            if not record or not record.get("dingtalk_synced"):
                return  # 未同步过的记录不需要重新同步

            roomid = record.get("roomid", "")
            sender = record.get("sender", "")
            monitors = self._get_matched_monitors(roomid, sender)
            for monitor in monitors:
                if not (monitor.get("dingtalk_base_id") and monitor.get("dingtalk_sheet_id")):
                    continue
                monitor_mapping = monitor.get("field_mapping") or {}
                if monitor_mapping:
                    from insurance.field_mapping import DEFAULT_MAPPING
                    export_to_ocr = {v: k for k, v in DEFAULT_MAPPING.items() if v}
                    sync_fields = {}
                    for export_col, target_col in monitor_mapping.items():
                        if not target_col:
                            continue
                        ocr_field = export_to_ocr.get(export_col, export_col)
                        if ocr_field in parsed_fields:
                            sync_fields[target_col] = parsed_fields[ocr_field]
                        elif export_col in parsed_fields:
                            sync_fields[target_col] = parsed_fields[export_col]
                else:
                    sync_fields = mapped_fields

                self._sync_to_dingtalk_v2(
                    record_id, sync_fields,
                    sender_name=record.get("sender_name", ""),
                    cos_url=record.get("cos_url", ""),
                    doc_category=record.get("doc_category", ""),
                    monitor=monitor,
                )
            logger.info("反向互补后重新同步钉钉: record_id=%d", record_id)
        except Exception as e:
            logger.warning("反向互补后重新同步钉钉失败: record_id=%d, %s", record_id, e)

    # ------------------------------------------------------------------ #
    # 重复 PDF 不同发送人：复制识别结果
    # ------------------------------------------------------------------ #

    def _copy_recognition_result(self, record_id: int, source_record: dict,
                                  file_md5: str, user_id, enterprise_id):
        """
        将已有记录的识别结果复制到当前记录（不同发送人转发同一 PDF）。
        同时处理多保单情况：源记录对应的所有兄弟记录也一并复制。
        """
        import json as _json
        from insurance.field_mapping import apply_mapping

        # 收集源 PDF 的所有记录（按 file_md5 查同一 PDF 的所有保单）
        source_records = [source_record]
        if file_md5:
            conn = self.db.pool.connection()
            try:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                # 关键修复：限定「同一次发送」(msg_seq 相同) 的保单组，避免把"多人各发一次
                # 同一 PDF"误当成"一份 PDF 含多个保单"——否则会凭空复制出张冠李戴的假记录。
                cursor.execute(
                    "SELECT * FROM insurance_records WHERE file_md5 = %s AND msg_seq = %s AND status = 'done' ORDER BY policy_index",
                    (file_md5, source_record.get("msg_seq"))
                )
                all_source = cursor.fetchall()
                if all_source:
                    source_records = all_source
            finally:
                conn.close()

        total_policies = len(source_records)

        for idx, src in enumerate(source_records):
            # 第一条复用已创建的 record_id，后续新建
            if idx == 0:
                cur_id = record_id
            else:
                # 兄弟记录（同一份PDF的其它保单）：发送信息(msg_seq/群/sender)一律用「当前这次
                # 发送」的，识别结果用源记录的，避免群/归属张冠李戴到别人或别的群
                cur_rec = get_insurance_record(self.db, record_id) or {}
                extra = {
                    "msg_seq": cur_rec.get("msg_seq", 0),
                    "roomid": cur_rec.get("roomid", ""),
                    "room_name": cur_rec.get("room_name", ""),
                    "sender": cur_rec.get("sender", ""),
                    "sender_name": cur_rec.get("sender_name", ""),
                    "filename": src.get("filename", ""),
                    "filesize": src.get("filesize", 0),
                    "file_md5": file_md5,
                    "status": "processing",
                    "source": "auto",
                    "user_id": user_id,
                    "enterprise_id": enterprise_id,
                }
                try:
                    cur_id = save_insurance_record(self.db, extra)
                except Exception as e:
                    logger.error("复制多保单第%d条记录失败: %s", idx + 1, e)
                    continue

            # 解析源记录的 parsed_fields
            pf = src.get("parsed_fields", {})
            if isinstance(pf, str):
                try:
                    pf = _json.loads(pf) if pf else {}
                except (_json.JSONDecodeError, TypeError):
                    pf = {}

            cs = pf.get("保险公司简称", "")
            mf = apply_mapping(pf, cs)

            updates = {
                "status": "done",
                "ocr_engine": src.get("ocr_engine", ""),
                "doc_category": src.get("doc_category", ""),
                "confidence": src.get("confidence", 0.0),
                "parsed_fields": pf,
                "mapped_fields": mf,
                "raw_text": src.get("raw_text", ""),
                "error_message": "",
                "cos_url": src.get("cos_url", ""),
                "file_md5": file_md5,
                "policy_count": total_policies,
                "policy_index": src.get("policy_index", idx + 1),
                "page_range": src.get("page_range", ""),
                "user_id": user_id,
                "enterprise_id": enterprise_id,
            }
            update_insurance_record(self.db, cur_id, updates)
            logger.info(
                "[COPY] 复制识别结果到 record_id=%d (policy %d/%d, 源记录=%d)",
                cur_id, idx + 1, total_policies, src["id"],
            )

    # ------------------------------------------------------------------ #
    # COS 上传
    # ------------------------------------------------------------------ #

    def _upload_to_cos(
        self, pdf_bytes: bytes, roomid: str, sender: str, filename: str
    ) -> str:
        """
        将 PDF 上传到 COS，返回访问 URL。

        COS 路径: insurance/{roomid}/{sender}/{filename}
        若同名文件已存在，追加毫秒时间戳区分。

        Args:
            pdf_bytes: PDF 字节内容
            roomid:    群 ID
            sender:    发送人 ID
            filename:  原始文件名

        Returns:
            COS URL 字符串，cos_storage 为 None 时返回 ""
        """
        if not self.cos_storage:
            return ""

        try:
            # 构建 COS key 后缀
            key_suffix = f"insurance/{roomid}/{sender}/{filename}"

            # 重名处理：追加毫秒时间戳
            if self.cos_storage.file_exists(key_suffix):
                name, ext = os.path.splitext(filename)
                ts = int(time.time() * 1000)
                key_suffix = f"insurance/{roomid}/{sender}/{name}_{ts}{ext}"

            url = self.cos_storage.upload_bytes(pdf_bytes, key_suffix)
            logger.debug("COS 上传成功: %s", url)
            return url or ""
        except Exception as e:
            logger.error("COS 上传失败: %s", e, exc_info=True)
            return ""

    # ------------------------------------------------------------------ #
    # OCR 流水线
    # ------------------------------------------------------------------ #

    def _run_ocr_engines(self, pdf_bytes: bytes) -> str:
        """调用 OCR 引擎提取文本（火山引擎优先，降级百度），返回文本或空字符串"""
        volc_circuit_open = (time.time() < self._volc_circuit_open_until)
        if self._volc_ocr and not volc_circuit_open:
            try:
                # 传入百度 OCR 作为单页兜底（火山引擎 429 限流重试耗尽时降级）
                _page_fb = self._baidu_ocr.recognize_pdf_bytes if self._baidu_ocr else None
                volc_result = self._volc_ocr.recognize_pdf_multi_pages(
                    pdf_bytes, max_pages=10, early_stop_check=_ocr_early_stop_check,
                    page_fallback=_page_fb)
                if volc_result.get("success"):
                    self._volc_fail_count = 0
                    text = volc_result.get("text", "")
                    logger.info("火山引擎 OCR 识别成功，文字数=%d", len(text))
                    return text
                self._volc_fail_count += 1
                _fail_reason = str(volc_result.get("error", "") or "未知")
                logger.warning("火山引擎 OCR 识别失败(%d/%d): %s",
                               self._volc_fail_count, self._volc_fail_threshold,
                               _fail_reason)
            except Exception as e:
                self._volc_fail_count += 1
                _fail_reason = str(e) or "未知异常"
                logger.error("火山引擎 OCR 调用异常(%d/%d): %s",
                             self._volc_fail_count, self._volc_fail_threshold, e)
            if self._volc_fail_count >= self._volc_fail_threshold:
                now = time.time()
                self._volc_circuit_open_until = now + 600
                # 只保留近 30 分钟内的熔断记录，用于降噪：单次熔断属正常降级(百度兜底)，频繁才告警
                self._volc_circuit_open_times = [t for t in self._volc_circuit_open_times if now - t < 1800]
                self._volc_circuit_open_times.append(now)
                trips = len(self._volc_circuit_open_times)
                logger.warning("火山引擎 OCR 失败熔断 10 分钟（30分钟内第 %d 次），原因: %s", trips, _fail_reason)
                # 30 分钟内反复熔断(>=3 次)才发钉钉告警，并带上失败原因
                if trips >= 3:
                    notify_error("OCR引擎", "_run_ocr_engines",
                                 f"火山引擎OCR 30分钟内已熔断{trips}次，最近原因: {_fail_reason}",
                                 "已自动降级百度OCR，识别未中断；请检查火山引擎配额/网络/密钥")
        elif volc_circuit_open:
            logger.info("火山引擎 OCR 熔断中，跳过直接使用百度 OCR")
        if self._baidu_ocr:
            try:
                baidu_result = self._baidu_ocr.recognize_pdf_multi_pages(
                    pdf_bytes, max_pages=10, early_stop_check=_ocr_early_stop_check)
                if baidu_result.get("success"):
                    text = baidu_result.get("text", "")
                    logger.info("百度 OCR 识别成功，文字数=%d", len(text))
                    return text
                logger.warning("百度 OCR 识别失败: %s", baidu_result.get("error", ""))
            except Exception as e:
                logger.error("百度 OCR 调用异常: %s", e, exc_info=True)
        return ""

    def _do_ocr(self, pdf_bytes: bytes, filename: str) -> dict:
        """
        OCR 识别流水线：pdfplumber 提取文本 → 质量检查 → 降级百度 OCR → 解析保单字段。
        支持一个 PDF 包含多份保单（如交强险+商业险），自动拆分为多条记录。

        Args:
            pdf_bytes: PDF 字节内容
            filename:  文件名（仅用于日志）

        Returns:
            {
                "success":    bool,
                "policies":   list,   # parse_policy_text_multi 的返回值（多条保单列表）
                "policy":     dict,   # 兼容：取第一条保单（向后兼容）
                "ocr_engine": str,
                "error":      str,
            }
        """
        text = ""
        ocr_engine = "pdfplumber"

        # --- 第一步：pdfplumber 提取文本层 ---
        try:
            plumber_result = extract_text_from_pdf_bytes(pdf_bytes)
            if plumber_result.get("success"):
                text = plumber_result.get("text", "")
            else:
                logger.info(
                    "pdfplumber 未提取到文本层（%s），降级百度 OCR",
                    plumber_result.get("error", ""),
                )
        except Exception as e:
            logger.warning("pdfplumber 提取失败: %s，降级百度 OCR", e)

        # --- 质量检查：文本质量不佳时降级 ---
        needs_fallback = not text
        if text:
            # 乱码检测：检查文本中的异常字符
            garbled_count = 0
            has_nonchar = False  # U+FFFE/U+FFFF 非字符（正常文本绝不会出现）
            for ch in text:
                cp = ord(ch)
                if cp in (0xFFFE, 0xFFFF):  # Unicode非字符，字体编码异常的确定性标志
                    has_nonchar = True
                    garbled_count += 1
                elif cp == 0xFFFD:  # 替换字符
                    garbled_count += 1
                elif 0xE000 <= cp <= 0xF8FF:  # 私有使用区
                    garbled_count += 1
                elif 0xFDD0 <= cp <= 0xFDEF:  # Unicode非字符区
                    has_nonchar = True
                    garbled_count += 1
                elif cp < 0x20 and ch not in '\n\r\t':  # 控制字符
                    garbled_count += 1
            # U+FFFE/U+FFFF 出现时：不再按保司白名单判断，而是清理非字符后用解析质量决定。
            # 很多保司（人保、太平洋等）PDF 虽含非字符，但清理后文本完全可用；若直接降级 OCR
            # 反而引入 OCR 误识（如车牌 0→O）。仅当清理后文本仍解析不出有效保单时才降级。
            garbled_ratio = garbled_count / len(text) if text else 0
            if has_nonchar:
                cleaned = ''.join(ch for ch in text if ord(ch) not in
                                  (0xFFFE, 0xFFFF) and not (0xFDD0 <= ord(ch) <= 0xFDEF))
                keep_pdfplumber = False
                try:
                    prelim = parse_policy_text(cleaned)
                    conf = prelim.get("confidence", 0)
                    cat = prelim.get("doc_category", "")
                    keep_pdfplumber = conf >= 0.4 and cat != "其他"
                except Exception as e:
                    logger.warning("非字符清理后质量检查失败: %s", e)
                if keep_pdfplumber:
                    # 清理非字符后继续使用 pdfplumber 文本（比 OCR 更准确）
                    text = cleaned
                    logger.info(
                        "pdfplumber 含Unicode非字符（count=%d）但清理后文本可用（confidence=%.2f），继续使用",
                        garbled_count, conf,
                    )
                else:
                    logger.info(
                        "pdfplumber 含Unicode非字符且清理后仍无法解析有效保单，降级 OCR（count=%d）",
                        garbled_count,
                    )
                    needs_fallback = True
            elif garbled_ratio > 0.05:
                logger.info(
                    "pdfplumber 提取文本含大量乱码（占比=%.1f%%, count=%d），降级 OCR",
                    garbled_ratio * 100, garbled_count,
                )
                needs_fallback = True
            else:
                try:
                    preliminary = parse_policy_text(text)
                    conf = preliminary.get("confidence", 0)
                    cat = preliminary.get("doc_category", "")
                    if conf < 0.4 or cat == "其他":
                        logger.info(
                            "pdfplumber 识别质量不足（confidence=%.2f, category=%s），降级 OCR",
                            conf, cat,
                        )
                        needs_fallback = True
                except Exception as e:
                    logger.warning("初步质量检查失败: %s，降级百度 OCR", e)
                    needs_fallback = True

        # --- 第二步（可选降级）：OCR 完全替换 pdfplumber 文本 ---
        if needs_fallback:
            ocr_text = self._run_ocr_engines(pdf_bytes)
            if ocr_text:
                text = ocr_text
                ocr_engine = "ocr"
                logger.info("OCR 降级成功，文字数=%d", len(text))
            else:
                logger.warning("所有 OCR 引擎均不可用或识别失败")

        if not text:
            return {
                "success": False,
                "policies": [],
                "policy": {},
                "ocr_engine": ocr_engine,
                "error": "OCR 未能提取到任何文字",
            }

        # --- 第三步：解析保单字段（支持多保单拆分） ---
        try:
            policies = parse_policy_text_multi(text)
        except Exception as e:
            logger.error("保单字段解析失败: %s", e, exc_info=True)
            return {
                "success": False,
                "policies": [],
                "policy": {},
                "ocr_engine": ocr_engine,
                "error": f"保单字段解析失败: {e}",
            }

        if len(policies) > 1:
            logger.info("检测到多保单PDF: %s, 共%d份保单", filename, len(policies))

        # --- 第四步：pdfplumber 关键字段缺失时，补充 OCR ---
        _SUPPLEMENT_KEYS = ['保险公司', '保单号', '险种类型', '车牌号', '投保人', '被保险人',
                            '签单日期', '保险起期', '保险止期', '保费合计']
        ocr_raw_text = ""
        if ocr_engine == "pdfplumber" and policies:
            first_fields = policies[0].get("fields", {})
            missing_keys = [k for k in _SUPPLEMENT_KEYS if not first_fields.get(k)]
            # 仅当缺失字段达到 5 个(含)以上才触发 OCR 补充，避免个别字段缺失就无谓调用 OCR
            if len(missing_keys) >= 5:
                ocr_text = self._run_ocr_engines(pdf_bytes)
                if ocr_text:
                    ocr_raw_text = ocr_text
                    try:
                        ocr_policies = parse_policy_text_multi(ocr_text)
                        if ocr_policies:
                            ocr_fields = ocr_policies[0].get("fields", {})
                            filled = []
                            for key in missing_keys:
                                if ocr_fields.get(key):
                                    first_fields[key] = ocr_fields[key]
                                    filled.append(key)
                            if filled:
                                ocr_engine = "pdfplumber+ocr"
                                logger.info("OCR 补充了 pdfplumber 缺失字段: %s", ", ".join(filled))
                            else:
                                logger.info("OCR 补充未能填充缺失字段: %s", ", ".join(missing_keys))
                    except Exception as e:
                        logger.warning("OCR 补充解析失败: %s", e)

        return {
            "success": True,
            "policies": policies,
            "policy": policies[0] if policies else {},
            "ocr_engine": ocr_engine,
            "ocr_text": ocr_raw_text,
            "error": "",
        }

    # ------------------------------------------------------------------ #
    # 同步到钉钉
    # ------------------------------------------------------------------ #

    def _sync_to_dingtalk_v2(
        self, record_id: int, sync_fields: dict,
        sender_name: str = "", cos_url: str = "",
        doc_category: str = "", monitor: dict = None,
    ):
        """
        按监控配置同步到指定钉钉文档。

        Args:
            record_id:    数据库记录 ID
            sync_fields:  经过字段映射后的导出字段
            sender_name:  发送人姓名
            cos_url:      COS 文件链接
            doc_category: 文档类型（保单/电子标志等）
            monitor:      匹配的监控配置（含 dingtalk_base_id, dingtalk_sheet_id 等）
        """
        from insurance.dingtalk_sync import DingTalkTableService

        if not monitor:
            return

        try:
            # 读取钉钉应用凭证
            app_cfg = self.app_config.get("dingtalk", {})
            if not app_cfg or not app_cfg.get("app_key"):
                app_cfg = get_insurance_config(self.db, "dingtalk_app", {})
            if not isinstance(app_cfg, dict):
                return

            app_key = app_cfg.get("app_key", "")
            app_secret = app_cfg.get("app_secret", "")
            operator_id = app_cfg.get("operator_id", "")

            if not (app_key and app_secret and operator_id):
                logger.warning("钉钉应用凭证未完整配置")
                return

            base_id = monitor["dingtalk_base_id"]
            sheet_id = monitor["dingtalk_sheet_id"]

            invoice_fields = {k: v for k, v in sync_fields.items() if v}
            if sender_name:
                invoice_fields["发送人"] = sender_name
            if cos_url:
                invoice_fields["保单文件"] = cos_url
            if doc_category:
                invoice_fields["文档类型"] = doc_category
            field_names = list(invoice_fields.keys())
            invoice = {"fields": invoice_fields}

            svc = DingTalkTableService(app_key, app_secret, base_id, sheet_id, operator_id)
            result = svc.append_invoices(field_names, [invoice])

            logger.info(
                "同步钉钉成功(v2), record_id=%d monitor=%s inserted=%s",
                record_id, monitor.get("name", monitor.get("id")),
                result.get("inserted_count", 0),
            )

            update_insurance_record(self.db, record_id, {
                "dingtalk_synced": 1,
                "dingtalk_target_id": str(monitor.get("id", "")),
            })

        except Exception as e:
            logger.error(
                "同步钉钉失败(v2), record_id=%d monitor=%s: %s",
                record_id, monitor.get("name", ""), e, exc_info=True,
            )

    # ------------------------------------------------------------------ #
    # 手动上传接口（同步）
    # ------------------------------------------------------------------ #

    def process_manual_upload(
        self,
        pdf_bytes: bytes,
        filename: str,
        file_type: str = "pdf",
        pdf_page: int = 1,
    ) -> dict:
        """
        手动上传识别（同步执行，立即返回结果）。

        - 图片（file_type != 'pdf'）走百度 OCR
        - PDF 走标准 pdfplumber → 降级百度 OCR 流水线
        - 上传 COS 路径: insurance/manual/{filename}
        - 保存识别记录（source=manual）

        Args:
            pdf_bytes: 文件字节内容
            filename:  原始文件名
            file_type: 'pdf' 或 'image'
            pdf_page:  PDF 识别页码（默认第 1 页）

        Returns:
            兼容原 baoxianOcr 格式：
            {"success", "file_name", "invoices": [policy], "ocr_engine"}
        """
        logger.info("手动上传识别, filename=%s file_type=%s", filename, file_type)

        cos_url = ""
        ocr_engine = "unknown"
        policy = {}
        error_msg = ""
        manual_ocr_text = ""

        try:
            # --- 图片走 OCR（优先火山引擎，降级百度）---
            if file_type != "pdf":
                import base64
                img_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                text = ""

                # 优先火山引擎（熔断期内跳过）
                if self._volc_ocr and time.time() >= self._volc_circuit_open_until:
                    try:
                        volc_result = self._volc_ocr.recognize_image(img_b64)
                        if volc_result.get("success"):
                            text = volc_result.get("text", "")
                            ocr_engine = "volc"
                            self._volc_fail_count = 0
                        else:
                            self._volc_fail_count += 1
                    except Exception as e:
                        self._volc_fail_count += 1
                        logger.warning("火山引擎图片识别失败: %s", e)
                    if self._volc_fail_count >= self._volc_fail_threshold:
                        self._volc_circuit_open_until = time.time() + 600
                        logger.warning("火山引擎 OCR 连续失败 %d 次，熔断 10 分钟", self._volc_fail_count)
                        notify_error("OCR引擎", "手动上传OCR", f"火山引擎OCR连续失败{self._volc_fail_count}次，已熔断10分钟")

                # 降级百度 OCR
                if not text and self._baidu_ocr:
                    baidu_result = self._baidu_ocr.recognize_image(img_b64)
                    if baidu_result.get("success"):
                        text = baidu_result.get("text", "")
                        ocr_engine = "baidu"

                if not text:
                    raise RuntimeError("所有 OCR 引擎均无法识别图片")
            else:
                # --- PDF 走标准流水线 ---
                ocr_result = self._do_ocr(pdf_bytes, filename)
                if not ocr_result.get("success"):
                    raise RuntimeError(ocr_result.get("error", "OCR 识别失败"))

                text = ""  # _do_ocr 内部已处理，通过 policy 返回
                ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")
                policy = ocr_result.get("policy", {})
                manual_ocr_text = ocr_result.get("ocr_text", "")

            # 图片 OCR 需要单独解析字段
            if file_type != "pdf" and text:
                try:
                    policy = parse_policy_text(text)
                except Exception as e:
                    logger.error("手动上传字段解析失败: %s", e)
                    policy = {"fields": {}, "doc_category": "", "confidence": 0.0}

            parsed_fields = policy.get("fields", {}) if policy else {}
            doc_category = policy.get("doc_category", "") if policy else ""
            confidence = policy.get("confidence", 0.0) if policy else 0.0

            # 字段映射
            company_short = parsed_fields.get("保险公司简称", "")
            mapped_fields = apply_mapping(parsed_fields, company_short)

            # 上传 COS（manual 路径）
            if self.cos_storage:
                try:
                    key_suffix = f"insurance/manual/{filename}"
                    if self.cos_storage.file_exists(key_suffix):
                        name, ext = os.path.splitext(filename)
                        ts = int(time.time() * 1000)
                        key_suffix = f"insurance/manual/{name}_{ts}{ext}"
                    cos_url = self.cos_storage.upload_bytes(pdf_bytes, key_suffix) or ""
                except Exception as e:
                    logger.warning("手动上传 COS 失败: %s", e)

            # 保存记录（source=manual）
            try:
                raw_text = policy.get("raw_text", "") if policy else ""
                record = {
                    "filename": filename,
                    "filesize": len(pdf_bytes),
                    "cos_url": cos_url,
                    "ocr_engine": ocr_engine,
                    "doc_category": doc_category,
                    "confidence": confidence,
                    "parsed_fields": parsed_fields,
                    "mapped_fields": mapped_fields,
                    "raw_text": raw_text,
                    "status": "done",
                    "source": "manual",
                }
                if file_type == "pdf" and manual_ocr_text:
                    record["ocr_text"] = manual_ocr_text
                save_insurance_record(self.db, record)
            except Exception as e:
                logger.warning("手动上传记录保存失败: %s", e)

            # 构建兼容返回格式（与原 baoxianOcr 接口保持兼容）
            raw_text_for_return = policy.get("raw_text", "") if policy else ""
            invoice = {"fields": mapped_fields} if mapped_fields else {}
            if raw_text_for_return:
                invoice["raw_text"] = raw_text_for_return
            if file_type == "pdf" and manual_ocr_text:
                invoice["ocr_text"] = manual_ocr_text
            return {
                "success": True,
                "file_name": filename,
                "invoices": [invoice] if invoice else [],
                "ocr_engine": ocr_engine,
            }

        except Exception as e:
            error_msg = str(e)
            logger.error("手动上传识别失败, filename=%s: %s", filename, e, exc_info=True)

            # 保存失败记录
            try:
                save_insurance_record(self.db, {
                    "filename": filename,
                    "filesize": len(pdf_bytes) if pdf_bytes else 0,
                    "cos_url": cos_url,
                    "status": "failed",
                    "error_message": error_msg[:500],
                    "source": "manual",
                })
            except Exception as se:
                logger.warning("保存失败记录出错: %s", se)

            return {
                "success": False,
                "file_name": filename,
                "invoices": [],
                "ocr_engine": ocr_engine,
                "error": error_msg,
            }
        finally:
            # 释放内存
            del pdf_bytes
            gc.collect()
