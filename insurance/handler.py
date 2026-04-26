#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保单识别处理器
负责：消息队列消费、媒体文件下载、OCR 识别、字段映射、COS 存储、钉钉同步
"""

import gc
import logging
import os
import queue
import tempfile
import threading
import time
from typing import Optional

from insurance.db import (
    get_insurance_config,
    save_insurance_record,
    update_insurance_record,
)
from insurance.field_mapping import apply_mapping
from insurance.ocr_service import extract_text_from_pdf_bytes
from insurance.policy_parser import parse_policy_text

logger = logging.getLogger(__name__)

# 超过该大小（20MB）的文件走临时文件下载
MAX_MEMORY_SIZE = 20 * 1024 * 1024


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

        # 内部消息队列（最多 100 条）
        self._queue: queue.Queue = queue.Queue(maxsize=100)

        # 百度 OCR 客户端（优先从 config.yaml 读取）
        self._baidu_ocr = None
        self._init_baidu_ocr()

        # 新识别引擎（渐进式启用）
        self._pipeline = None
        try:
            from insurance.engine.pipeline import InsurancePipeline
            from insurance.ocr.baidu_engine import BaiduOCREngine
            ocr_engines = []
            baidu_config = app_config.get("baidu_ocr", {})
            if baidu_config.get("api_key"):
                ocr_engines.append(BaiduOCREngine(
                    baidu_config["api_key"], baidu_config["secret_key"]))
            tencent_config = app_config.get("tencent_ocr", {})
            if tencent_config.get("secret_id"):
                from insurance.ocr.tencent_engine import TencentOCREngine
                ocr_engines.append(TencentOCREngine(
                    tencent_config["secret_id"], tencent_config["secret_key"]))
            self._pipeline = InsurancePipeline(
                db=db, ocr_engines=ocr_engines,
                alert_webhook=app_config.get("dingtalk", {}).get("alert_webhook", ""))
            logger.info("新识别引擎初始化成功")
        except Exception as e:
            logger.warning(f"新识别引擎初始化失败，使用旧逻辑: {e}")
            self._pipeline = None

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

            # 优先从 config.yaml 读取
            cfg = self.app_config.get("baidu_ocr", {})
            if not cfg or not cfg.get("api_key"):
                # 降级从数据库读取
                cfg = get_insurance_config(self.db, "baidu_ocr", {})
            api_key = cfg.get("api_key", "") if isinstance(cfg, dict) else ""
            secret_key = cfg.get("secret_key", "") if isinstance(cfg, dict) else ""

            if api_key and secret_key:
                self._baidu_ocr = BaiduOCR(api_key, secret_key)
                logger.info("百度 OCR 客户端初始化成功")
            else:
                self._baidu_ocr = None
                logger.warning("百度 OCR 未配置 api_key / secret_key，降级功能不可用")
        except Exception as e:
            self._baidu_ocr = None
            logger.error("百度 OCR 初始化失败: %s", e)

    def reload_baidu_ocr(self):
        """公开方法：重新从数据库读取配置并重建百度 OCR 客户端。"""
        logger.info("重新加载百度 OCR 配置")
        self._init_baidu_ocr()

    # ------------------------------------------------------------------ #
    # 监控群列表
    # ------------------------------------------------------------------ #

    def get_watch_config(self) -> dict:
        """
        从数据库配置获取监控列表（群 + 私聊）。

        配置格式: [{"id": "xxx", "name": "xxx", "type": "room"|"user"}, ...]
        返回: {"rooms": [roomid, ...], "users": [userid, ...]}
        """
        try:
            watch_list = get_insurance_config(self.db, "watch_list", [])
            if not isinstance(watch_list, list):
                # 兼容旧格式 watch_rooms
                watch_rooms = get_insurance_config(self.db, "watch_rooms", [])
                if isinstance(watch_rooms, list):
                    return {
                        "rooms": [r.get("roomid", "") for r in watch_rooms if isinstance(r, dict) and r.get("roomid")],
                        "users": [],
                    }
                return {"rooms": [], "users": []}
            rooms = [w["id"] for w in watch_list if isinstance(w, dict) and w.get("type") == "room" and w.get("id")]
            users = [w["id"] for w in watch_list if isinstance(w, dict) and w.get("type") == "user" and w.get("id")]
            return {"rooms": rooms, "users": users}
        except Exception as e:
            logger.error("获取监控列表失败: %s", e)
            return {"rooms": [], "users": []}

    def get_watch_rooms(self) -> list:
        """兼容方法，返回监控群 ID 列表"""
        return self.get_watch_config().get("rooms", [])

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
                "保单识别队列已满（maxsize=100），丢弃消息 seq=%s filename=%s",
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
            try:
                room_name = self.contacts.get_room_name(roomid) or ""
            except Exception as e:
                logger.warning("获取群名失败: %s", e)

        # 2. 创建初始记录（状态: processing）
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
        }
        try:
            record_id = save_insurance_record(self.db, initial_record)
        except Exception as e:
            logger.error("创建保单记录失败: %s", e)
            return

        pdf_bytes = b""
        cos_url = ""
        try:
            # 3. 下载媒体文件
            pdf_bytes = self._download_media(sdkfileid, filesize)
            if not pdf_bytes:
                raise RuntimeError("媒体文件下载失败或内容为空")

            # 4. 上传 COS
            cos_url = self._upload_to_cos(pdf_bytes, roomid, sender, filename)

            # 5. OCR 识别
            ocr_result = self._do_ocr(pdf_bytes, filename)
            if not ocr_result.get("success"):
                raise RuntimeError(f"OCR 识别失败: {ocr_result.get('error', '未知错误')}")

            policy = ocr_result.get("policy", {})
            ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")
            parsed_fields = policy.get("fields", {})
            doc_category = policy.get("doc_category", "")
            confidence = policy.get("confidence", 0.0)

            # 6. 字段映射
            company_short = parsed_fields.get("保险公司简称", "")
            mapped_fields = apply_mapping(parsed_fields, company_short)

            # 7. 更新记录为 done
            updates = {
                "status": "done",
                "cos_url": cos_url,
                "ocr_engine": ocr_engine,
                "doc_category": doc_category,
                "confidence": confidence,
                "parsed_fields": parsed_fields,
                "mapped_fields": mapped_fields,
            }
            update_insurance_record(self.db, record_id, updates)

            # 保存新引擎的额外信息
            pipeline_result = ocr_result.get("pipeline_result")
            if pipeline_result:
                import json as _json
                extra_updates = {
                    "template_id": pipeline_result.template_id,
                    "match_score": pipeline_result.match_score,
                    "field_layer_data": _json.dumps({
                        "common": pipeline_result.fields.get("common", {}),
                        "type_specific": pipeline_result.fields.get("type_specific", {}),
                        "company_specific": pipeline_result.fields.get("company_specific", {}),
                    }, ensure_ascii=False),
                    "ocr_engines": ",".join(pipeline_result.ocr_engines),
                    "ocr_cross_validated": 1 if pipeline_result.ocr_diffs else 0,
                    "ocr_diffs": _json.dumps([{
                        "field": d.field_name, "values": d.values,
                        "is_critical": d.is_critical, "resolved": d.resolved_value,
                    } for d in pipeline_result.ocr_diffs], ensure_ascii=False) if pipeline_result.ocr_diffs else None,
                    "has_critical_diff": 1 if any(d.is_critical for d in pipeline_result.ocr_diffs) else 0,
                }
                update_insurance_record(self.db, record_id, extra_updates)

            logger.info("保单处理完成, record_id=%d filename=%s", record_id, filename)

            # 8. 自动同步钉钉（按配置决定）
            try:
                auto_sync = get_insurance_config(self.db, "auto_sync_enabled", False)
                if auto_sync:
                    self._sync_to_dingtalk(
                        record_id, mapped_fields, parsed_fields,
                        sender_name=sender_name, roomid=roomid, sender=sender,
                    )
            except Exception as e:
                logger.error("自动同步钉钉失败, record_id=%d: %s", record_id, e)

        except Exception as e:
            logger.error("保单处理失败, record_id=%d filename=%s: %s", record_id, filename, e, exc_info=True)
            try:
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "error_message": str(e)[:500],
                    "cos_url": cos_url,
                })
            except Exception as ue:
                logger.error("更新失败状态时出错: %s", ue)
        finally:
            # 释放内存
            del pdf_bytes
            gc.collect()

    # ------------------------------------------------------------------ #
    # 媒体下载
    # ------------------------------------------------------------------ #

    def _download_media(self, sdkfileid: str, filesize: int) -> bytes:
        """
        从企业微信 SDK 下载媒体文件。

        - filesize > MAX_MEMORY_SIZE 时走临时文件
        - 否则直接下载到内存

        Args:
            sdkfileid: SDK 文件 ID
            filesize:  文件大小（字节）

        Returns:
            文件字节内容，失败返回 b""
        """
        if not self.finance_sdk:
            logger.warning("finance_sdk 未初始化，无法下载媒体文件")
            return b""

        proxy = self.sdk_config.get("proxy", "")
        passwd = self.sdk_config.get("proxy_passwd", "")
        timeout = self.sdk_config.get("timeout", 30)

        try:
            if filesize > MAX_MEMORY_SIZE:
                # 大文件走临时文件，避免占用大量内存
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
                os.close(tmp_fd)
                try:
                    ret, _ = self.finance_sdk.get_media_data(
                        sdkfileid, proxy, passwd, timeout, tmp_path
                    )
                    if ret != 0:
                        logger.error("下载大文件失败, ret=%d", ret)
                        return b""
                    with open(tmp_path, "rb") as f:
                        return f.read()
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            else:
                # 小文件直接下载到内存
                ret, data = self.finance_sdk.get_media_data_bytes(
                    sdkfileid, proxy, passwd, timeout
                )
                if ret != 0:
                    logger.error("下载媒体文件失败, ret=%d", ret)
                    return b""
                return data if data else b""

        except Exception as e:
            logger.error("下载媒体文件异常, sdkfileid=%s: %s", sdkfileid, e, exc_info=True)
            return b""

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

    def _do_ocr(self, pdf_bytes: bytes, filename: str) -> dict:
        """
        OCR 识别流水线：pdfplumber 提取文本 → 质量检查 → 降级百度 OCR → 解析保单字段。

        Args:
            pdf_bytes: PDF 字节内容
            filename:  文件名（仅用于日志）

        Returns:
            {
                "success":    bool,
                "policy":     dict,   # parse_policy_text 的返回值
                "ocr_engine": str,
                "error":      str,
            }
        """
        # 优先使用新引擎
        if self._pipeline:
            try:
                result = self._pipeline.process_pdf(pdf_bytes, filename)
                if result.confidence > 0:
                    # 新引擎成功，转换为旧格式兼容
                    flat_fields = {}
                    flat_fields.update(result.fields.get("common", {}))
                    flat_fields.update(result.fields.get("type_specific", {}))
                    flat_fields.update(result.fields.get("company_specific", {}))
                    flat_fields["保险公司"] = result.company.company_name
                    flat_fields["保险公司简称"] = result.company.company_short
                    flat_fields["险种类型"] = result.policy_type.type_name
                    flat_fields["文档类型"] = result.doc_category

                    policy = {
                        "type": result.policy_type.type_name,
                        "type_code": result.policy_type.type_code,
                        "confidence": result.confidence,
                        "doc_category": result.doc_category,
                        "fields": flat_fields,
                        "insurance_items": result.insurance_items,
                        "raw_text": result.raw_text,
                    }
                    return {
                        "success": True,
                        "policy": policy,
                        "ocr_engine": ",".join(result.ocr_engines),
                        "pipeline_result": result,
                    }
            except Exception as e:
                logger.warning(f"新引擎处理失败，降级到旧逻辑: {e}")

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
            try:
                preliminary = parse_policy_text(text)
                if preliminary.get("confidence", 0) == 0 or preliminary.get("doc_category", "") == "其他":
                    logger.info(
                        "pdfplumber 识别质量不足（confidence=%.2f, category=%s），降级百度 OCR",
                        preliminary.get("confidence", 0),
                        preliminary.get("doc_category", ""),
                    )
                    needs_fallback = True
            except Exception as e:
                logger.warning("初步质量检查失败: %s，降级百度 OCR", e)
                needs_fallback = True

        # --- 第二步（可选降级）：百度 OCR ---
        if needs_fallback:
            if self._baidu_ocr:
                try:
                    baidu_result = self._baidu_ocr.recognize_pdf_bytes(pdf_bytes, page_num=1)
                    if baidu_result.get("success"):
                        text = baidu_result.get("text", "")
                        ocr_engine = "baidu"
                        logger.info("百度 OCR 识别成功，文字数=%d", len(text))
                    else:
                        logger.warning("百度 OCR 识别失败: %s", baidu_result.get("error", ""))
                except Exception as e:
                    logger.error("百度 OCR 调用异常: %s", e, exc_info=True)
            else:
                logger.warning("百度 OCR 未配置，无法降级识别")

        if not text:
            return {
                "success": False,
                "policy": {},
                "ocr_engine": ocr_engine,
                "error": "OCR 未能提取到任何文字",
            }

        # --- 第三步：解析保单字段 ---
        try:
            policy = parse_policy_text(text)
        except Exception as e:
            logger.error("保单字段解析失败: %s", e, exc_info=True)
            return {
                "success": False,
                "policy": {},
                "ocr_engine": ocr_engine,
                "error": f"保单字段解析失败: {e}",
            }

        return {
            "success": True,
            "policy": policy,
            "ocr_engine": ocr_engine,
            "error": "",
        }

    # ------------------------------------------------------------------ #
    # 同步到钉钉
    # ------------------------------------------------------------------ #

    def _get_bound_targets(self, roomid: str, sender: str) -> list:
        """
        根据 roomid / sender 查找绑定的钉钉目标 ID 列表。
        watch_list 中每个监控项有 target_ids 字段。
        """
        watch_list = get_insurance_config(self.db, "watch_list", [])
        if not isinstance(watch_list, list):
            return []

        target_ids = set()
        for w in watch_list:
            if not isinstance(w, dict):
                continue
            wid = w.get("id", "")
            wtype = w.get("type", "")
            # 群聊匹配 roomid，私聊匹配 sender
            matched = False
            if wtype == "room" and roomid and wid == roomid:
                matched = True
            elif wtype == "user" and not roomid and sender and wid == sender:
                matched = True
            if matched:
                for tid in w.get("target_ids", []):
                    target_ids.add(tid)

        return list(target_ids)

    def _sync_to_dingtalk(
        self, record_id: int, mapped_fields: dict, parsed_fields: dict,
        sender_name: str = "", roomid: str = "", sender: str = "",
        target_ids: list = None,
    ):
        """
        将保单字段同步到钉钉多维表。

        根据 roomid/sender 查找绑定的目标文档，只同步到绑定的目标。
        同步时自动带上「发送人」字段。

        Args:
            record_id:     数据库记录 ID
            mapped_fields: apply_mapping 后的导出列字段
            parsed_fields: OCR 原始解析字段
            sender_name:   发送人姓名
            roomid:        来源群 ID
            sender:        发送人 ID
            target_ids:    指定目标 ID 列表（为 None 时根据绑定关系自动查找）
        """
        from insurance.dingtalk_sync import DingTalkTableService

        try:
            # 读取应用凭证（优先 config.yaml，其次数据库）
            app_cfg = self.app_config.get("dingtalk", {})
            if not app_cfg or not app_cfg.get("app_key"):
                app_cfg = get_insurance_config(self.db, "dingtalk_app", {})
            if not isinstance(app_cfg, dict):
                logger.warning("钉钉应用配置格式不正确")
                return

            app_key = app_cfg.get("app_key", "")
            app_secret = app_cfg.get("app_secret", "")
            operator_id = app_cfg.get("operator_id", "")

            if not (app_key and app_secret and operator_id):
                logger.warning("钉钉应用凭证未完整配置（app_key/app_secret/operator_id）")
                return

            # 确定要同步的目标
            if target_ids is None:
                target_ids = self._get_bound_targets(roomid, sender)

            if not target_ids:
                logger.debug("record_id=%d 无绑定的钉钉目标，跳过同步", record_id)
                return

            # 读取所有目标配置
            all_targets = get_insurance_config(self.db, "dingtalk_targets", [])
            if not isinstance(all_targets, list):
                return
            target_map = {t["id"]: t for t in all_targets if isinstance(t, dict) and t.get("id")}

            # 构建同步数据（带上发送人字段）
            invoice_fields = {k: v for k, v in mapped_fields.items() if v}
            if sender_name:
                invoice_fields["发送人"] = sender_name
            field_names = list(invoice_fields.keys())
            invoice = {"fields": invoice_fields}

            all_success = True
            synced_target_ids = []

            for tid in target_ids:
                target = target_map.get(tid)
                if not target:
                    logger.warning("钉钉目标 %s 不存在，跳过", tid)
                    continue

                base_id = target.get("base_id", "")
                sheet_id = target.get("sheet_id", "")
                if not (base_id and sheet_id):
                    continue

                try:
                    svc = DingTalkTableService(
                        app_key, app_secret, base_id, sheet_id, operator_id
                    )
                    result = svc.append_invoices(field_names, [invoice])
                    synced_target_ids.append(tid)
                    logger.info(
                        "同步钉钉成功, record_id=%d target=%s inserted=%s",
                        record_id, target.get("name", tid),
                        result.get("inserted_count", 0),
                    )
                except Exception as e:
                    all_success = False
                    logger.error(
                        "同步钉钉失败, record_id=%d target=%s: %s",
                        record_id, target.get("name", tid), e, exc_info=True,
                    )

            if synced_target_ids:
                update_insurance_record(self.db, record_id, {
                    "dingtalk_synced": 1 if all_success else 0,
                    "dingtalk_target_id": ",".join(synced_target_ids),
                })

        except Exception as e:
            logger.error("_sync_to_dingtalk 异常, record_id=%d: %s", record_id, e, exc_info=True)

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

        try:
            # --- 图片走百度 OCR ---
            if file_type != "pdf":
                import base64
                if not self._baidu_ocr:
                    raise RuntimeError("百度 OCR 未配置，无法识别图片")

                img_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                baidu_result = self._baidu_ocr.recognize_image(img_b64)
                if not baidu_result.get("success"):
                    raise RuntimeError(f"百度 OCR 识别失败: {baidu_result.get('error', '')}")

                text = baidu_result.get("text", "")
                ocr_engine = "baidu"
            else:
                # --- PDF 走标准流水线 ---
                ocr_result = self._do_ocr(pdf_bytes, filename)
                if not ocr_result.get("success"):
                    raise RuntimeError(ocr_result.get("error", "OCR 识别失败"))

                text = ""  # _do_ocr 内部已处理，通过 policy 返回
                ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")
                policy = ocr_result.get("policy", {})

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
                record = {
                    "filename": filename,
                    "filesize": len(pdf_bytes),
                    "cos_url": cos_url,
                    "ocr_engine": ocr_engine,
                    "doc_category": doc_category,
                    "confidence": confidence,
                    "parsed_fields": parsed_fields,
                    "mapped_fields": mapped_fields,
                    "status": "done",
                    "source": "manual",
                }
                save_insurance_record(self.db, record)
            except Exception as e:
                logger.warning("手动上传记录保存失败: %s", e)

            # 构建兼容返回格式（与原 baoxianOcr 接口保持兼容）
            return {
                "success": True,
                "file_name": filename,
                "invoices": [{"fields": mapped_fields}] if mapped_fields else [],
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
