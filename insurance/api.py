#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保单识别模块 — Flask API 蓝图
提供保单识别记录查询、手动上传识别、重试/同步、配置管理、字段映射、钉钉目标管理等接口
"""

import base64
import hashlib
import io
import json
import logging

import pymysql
import pymysql.cursors
from flask import Blueprint, jsonify, request, send_file

from .db import (
    get_all_insurance_config,
    get_insurance_config,
    get_insurance_record,
    get_insurance_stats,
    query_insurance_records,
    set_insurance_config,
    update_insurance_record,
)
from .monitor_config_db import (
    list_monitor_configs,
    get_monitor_config,
    create_monitor_config,
    update_monitor_config,
    delete_monitor_config,
)
from .dingtalk_sync import DingTalkTableService, parse_dingtalk_doc_url
from .field_mapping import (
    OUTPUT_COLUMNS,
    apply_mapping,
    get_available_ocr_fields,
    load_mapping_config,
    save_mapping_config,
)
from .policy_parser import get_extraction_rules, parse_policy_text
from .ocr_service import extract_text_from_pdf

logger = logging.getLogger(__name__)

# 创建蓝图，不设 url_prefix（路由中已写全路径）
insurance_bp = Blueprint("insurance", __name__)

# 模块级别的依赖注入变量
_db = None
_handler = None
_contacts = None


def init_insurance_api(db, handler=None, contacts=None):
    """
    初始化保单识别 API 所需的依赖。
    db      : 数据库实例（提供 db.pool.connection()）
    handler : InsuranceHandler 实例
    contacts: 联系人/群组解析服务实例
    """
    global _db, _handler, _contacts
    _db = db
    _handler = handler
    _contacts = contacts


# ============================================================
# 识别记录
# ============================================================

@insurance_bp.route("/api/insurance/records", methods=["GET"])
def list_records():
    """分页查询保单识别记录，支持按群、状态、关键词、来源筛选"""
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 20))
    roomid = request.args.get("roomid", "")
    status = request.args.get("status", "")
    keyword = request.args.get("keyword", "")
    source = request.args.get("source", "")

    try:
        result = query_insurance_records(
            _db,
            page=page,
            page_size=page_size,
            roomid=roomid,
            status=status,
            keyword=keyword,
            source=source,
        )
        # JSON 字段自动反序列化，避免前端收到字符串
        for record in result.get("records", []):
            for field in ("parsed_fields", "mapped_fields"):
                if isinstance(record.get(field), str):
                    try:
                        record[field] = json.loads(record[field])
                    except (TypeError, json.JSONDecodeError):
                        pass
            # 时间戳转字符串
            for ts_field in ("created_at", "updated_at"):
                if record.get(ts_field) and not isinstance(record[ts_field], str):
                    record[ts_field] = str(record[ts_field])

        return jsonify({"code": 0, "data": result})
    except Exception as e:
        logger.exception("查询保单记录失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/records/<int:record_id>", methods=["GET"])
def get_record(record_id):
    """获取单条保单识别记录详情"""
    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        # JSON 字段自动反序列化
        for field in ("parsed_fields", "mapped_fields"):
            if isinstance(record.get(field), str):
                try:
                    record[field] = json.loads(record[field])
                except (TypeError, json.JSONDecodeError):
                    pass
        # 时间戳转字符串
        for ts_field in ("created_at", "updated_at"):
            if record.get(ts_field) and not isinstance(record[ts_field], str):
                record[ts_field] = str(record[ts_field])

        return jsonify({"code": 0, "data": record})
    except Exception as e:
        logger.exception("获取保单记录 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/stats", methods=["GET"])
def get_stats():
    """获取保单识别统计信息"""
    try:
        stats = get_insurance_stats(_db)
        return jsonify({"code": 0, "data": stats})
    except Exception as e:
        logger.exception("获取保单统计失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 手动上传识别
# ============================================================

@insurance_bp.route("/api/insurance/parse", methods=["POST"])
def parse_text_only():
    """
    纯文本解析（不做 OCR），接收前端提取的文本直接解析保单字段。
    请求体: {text, file_name}
    """
    try:
        body = request.get_json(force=True) or {}
        text = body.get("text", "")
        file_name = body.get("file_name", "upload")

        if not text:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "文本为空", "invoices": [],
            })

        from .ocr_service import clean_text
        text = clean_text(text)
        policy = parse_policy_text(text)

        if not policy.get("fields") or policy.get("confidence", 0) == 0:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "need_ocr", "invoices": [],
            })

        return jsonify({
            "success": True,
            "file_name": file_name,
            "invoices": [policy],
            "char_count": len(text),
            "ocr_engine": "pdfjs",
        })
    except Exception as e:
        logger.exception("文本解析失败")
        return jsonify({
            "success": False, "file_name": file_name,
            "error": str(e), "invoices": [],
        })


@insurance_bp.route("/api/insurance/ocr", methods=["POST"])
def manual_ocr():
    """
    手动上传文件进行保单识别（纯识别，不上传 COS、不保存记录）。
    采用 baoxianOcr 的轻量逻辑：pdfplumber → 质量不佳降级百度 OCR → parse_policy_text
    请求体: {file_data(base64), file_type, file_name, pdf_page}
    """
    try:
        body = request.get_json(force=True) or {}
        file_data_b64 = body.get("file_data", "")
        file_type = body.get("file_type", "pdf")
        file_name = body.get("file_name", "upload")
        pdf_page = body.get("pdf_page", 0) or 0  # 0=提取所有页

        if not file_data_b64:
            return jsonify({"code": 400, "msg": "缺少 file_data 参数"}), 400

        # 初始化百度 OCR（从 handler 或配置中获取）
        baidu_ocr_client = getattr(_handler, "_baidu_ocr", None) if _handler else None

        extract_result = None
        ocr_engine = "pdfplumber"

        # 图片文件直接走百度 OCR
        if file_type in ("jpg", "jpeg", "png", "bmp"):
            if not baidu_ocr_client:
                return jsonify({
                    "success": False, "file_name": file_name,
                    "error": "图片识别需要配置百度OCR", "invoices": [],
                })
            extract_result = baidu_ocr_client.recognize_image(file_data_b64)
            ocr_engine = "baidu"
        else:
            # PDF：先用 pdfplumber 提取文本层
            extract_result = extract_text_from_pdf(
                file_data_base64=file_data_b64, pdf_page=pdf_page)

            # pdfplumber 失败（无文本层）→ 降级到百度 OCR
            if not extract_result["success"] and baidu_ocr_client:
                logger.info("[%s] pdfplumber 无文本层，降级使用百度OCR", file_name)
                extract_result = baidu_ocr_client.recognize_pdf(
                    file_data_b64, page_num=pdf_page)
                ocr_engine = "baidu"

        if not extract_result["success"]:
            error_msg = extract_result.get("error", "OCR 识别失败")
            if "无文本层" in error_msg and not baidu_ocr_client:
                error_msg += "（提示：配置百度OCR可自动识别扫描件）"
            return jsonify({
                "success": False, "file_name": file_name,
                "error": error_msg, "invoices": [],
            })

        policy = parse_policy_text(extract_result["text"])

        # 质量不佳时降级百度 OCR
        need_fallback = (
            not policy.get("fields")
            or policy.get("doc_category") == "其他"
            or policy.get("confidence", 0) == 0
        )
        if need_fallback and ocr_engine == "pdfplumber" and baidu_ocr_client:
            logger.info("[%s] pdfplumber 效果不佳(类型=%s, 置信度=%s), 降级百度OCR",
                        file_name, policy.get("doc_category"), policy.get("confidence"))
            extract_result = baidu_ocr_client.recognize_pdf(
                file_data_b64, page_num=pdf_page)
            ocr_engine = "baidu"
            if extract_result["success"]:
                policy = parse_policy_text(extract_result["text"])

        if not policy.get("fields") or policy.get("confidence", 0) == 0:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "未识别到任何保单关键字段", "invoices": [],
            })

        return jsonify({
            "success": True,
            "file_name": file_name,
            "invoices": [policy],
            "char_count": extract_result.get("char_count", 0),
            "ocr_engine": ocr_engine,
        })
    except Exception as e:
        logger.exception("手动上传识别失败")
        return jsonify({
            "success": False, "file_name": file_name,
            "error": str(e), "invoices": [],
        })


# ============================================================
# 重试 / 同步
# ============================================================

@insurance_bp.route("/api/insurance/retry/<int:record_id>", methods=["POST"])
def retry_record(record_id):
    """
    重试失败的保单识别记录。
    逻辑：从 messages 表取原始 sdkfileid → 删除旧记录 → 重新入队
    """
    if not _handler:
        return jsonify({"code": 503, "msg": "识别服务未初始化"}), 503

    try:
        # 1. 获取原始记录
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        # 2. 仅允许重试失败状态的记录
        if record.get("status") != "failed":
            return jsonify({"code": 400, "msg": f"当前状态 {record.get('status')} 不允许重试，只有 failed 状态可重试"}), 400

        # 3. 手动上传的记录没有 msg_seq，无法重试
        msg_seq = record.get("msg_seq")
        if not msg_seq:
            return jsonify({"code": 400, "msg": "手动上传的记录无法重试（缺少消息序号）"}), 400

        # 4. 从 messages 表获取原始 sdkfileid
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT parsed_content FROM messages WHERE seq = %s",
                (msg_seq,)
            )
            msg_row = cursor.fetchone()
        finally:
            conn.close()

        if not msg_row:
            return jsonify({"code": 404, "msg": f"未找到消息记录 seq={msg_seq}"}), 404

        # 解析 parsed_content 获取 sdkfileid
        parsed_content = msg_row.get("parsed_content") or "{}"
        if isinstance(parsed_content, str):
            try:
                parsed_content = json.loads(parsed_content)
            except (TypeError, json.JSONDecodeError):
                parsed_content = {}

        sdkfileid = parsed_content.get("sdkfileid", "")
        if not sdkfileid:
            return jsonify({"code": 400, "msg": "消息中未找到 sdkfileid，无法重试"}), 400

        # 5. 删除旧的识别记录
        conn2 = _db.pool.connection()
        try:
            cursor2 = conn2.cursor(pymysql.cursors.DictCursor)
            cursor2.execute("DELETE FROM insurance_records WHERE id = %s", (record_id,))
            conn2.commit()
        finally:
            conn2.close()

        # 6. 重新入队
        _handler.enqueue({
            "seq": msg_seq,
            "roomid": record.get("roomid", ""),
            "sender": record.get("sender", ""),
            "filename": record.get("filename", ""),
            "sdkfileid": sdkfileid,
            "filesize": record.get("filesize", 0),
        })

        return jsonify({"code": 0, "msg": "已重新加入识别队列"})
    except Exception as e:
        logger.exception("重试保单记录 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/sync/<int:record_id>", methods=["POST"])
def sync_record_to_dingtalk(record_id):
    """手动触发单条记录同步到钉钉"""
    if not _handler:
        return jsonify({"code": 503, "msg": "识别服务未初始化"}), 503

    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        result = _handler._sync_to_dingtalk(record)
        return jsonify({"code": 0, "data": result, "msg": "同步成功"})
    except Exception as e:
        logger.exception("同步记录 %d 到钉钉失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 配置管理
# ============================================================

@insurance_bp.route("/api/insurance/config", methods=["GET"])
def get_config():
    """获取全部保单识别配置"""
    try:
        config = get_all_insurance_config(_db)
        return jsonify({"code": 0, "data": config})
    except Exception as e:
        logger.exception("获取保单配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/config", methods=["PUT"])
def update_config():
    """
    更新保单识别配置。
    请求体: {key: value, ...}
    更新 baidu_ocr 相关配置时，自动重载 OCR 客户端。
    """
    try:
        body = request.get_json(force=True) or {}
        if not body:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

        # 判断是否涉及 baidu_ocr 配置变更
        baidu_ocr_keys = {"baidu_ocr_app_id", "baidu_ocr_api_key", "baidu_ocr_secret_key", "baidu_ocr_enabled"}
        need_reload_ocr = bool(set(body.keys()) & baidu_ocr_keys)

        # 逐个写入配置
        for key, value in body.items():
            set_insurance_config(_db, key, value)

        # 重载百度 OCR 客户端
        if need_reload_ocr and _handler:
            try:
                _handler.reload_baidu_ocr()
                logger.info("百度 OCR 配置已重载")
            except Exception as reload_err:
                logger.warning("百度 OCR 重载失败: %s", reload_err)

        return jsonify({"code": 0, "msg": "配置已更新"})
    except Exception as e:
        logger.exception("更新保单配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/rooms", methods=["GET"])
def get_rooms():
    """
    获取可监控的对象列表（群聊 + 私聊联系人）。
    先用 LEFT JOIN contacts_cache 快速查出已缓存的名称，
    再对未缓存的项补调通讯录 API 并写入缓存（下次就快了）。
    返回: {"rooms": [...], "users": [...]}
    """
    try:
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)

            # 群聊列表
            cursor.execute("""
                SELECT m.roomid AS id, c.name AS cached_name
                FROM (
                    SELECT DISTINCT roomid FROM messages
                    WHERE roomid IS NOT NULL AND roomid != ''
                ) m
                LEFT JOIN contacts_cache c ON c.id = m.roomid
                ORDER BY m.roomid
            """)
            room_rows = cursor.fetchall()

            # 私聊联系人
            cursor.execute("""
                SELECT m.sender AS id, c.name AS cached_name
                FROM (
                    SELECT DISTINCT sender FROM messages
                    WHERE (roomid IS NULL OR roomid = '') AND sender IS NOT NULL AND sender != ''
                ) m
                LEFT JOIN contacts_cache c ON c.id = m.sender
                ORDER BY m.sender
            """)
            user_rows = cursor.fetchall()
        finally:
            conn.close()

        # 对未缓存的项补调通讯录 API（会自动写入缓存）
        rooms = []
        for r in room_rows:
            name = r["cached_name"]
            # 过滤不可解析标记
            if name == "__unresolvable__":
                name = None
            if not name and _contacts:
                try:
                    name = _contacts.get_room_name(r["id"])
                except Exception:
                    pass
            rooms.append({"id": r["id"], "name": name or r["id"], "type": "room"})

        users = []
        for u in user_rows:
            name = u["cached_name"]
            if name == "__unresolvable__":
                name = None
            if not name and _contacts:
                try:
                    name = _contacts.get_name(u["id"])
                except Exception:
                    pass
            users.append({"id": u["id"], "name": name or u["id"], "type": "user"})

        # 按名称排序
        rooms.sort(key=lambda x: x["name"])
        users.sort(key=lambda x: x["name"])

        return jsonify({"rooms": rooms, "users": users})
    except Exception as e:
        logger.exception("获取监控对象列表失败")
        return jsonify({"rooms": [], "users": []})


@insurance_bp.route("/api/insurance/sync-contacts", methods=["POST"])
def sync_contacts():
    """
    手动同步通讯录：强制刷新所有群和联系人的名称缓存。
    遍历 messages 表中所有 roomid 和 sender，逐个调企微 API 更新缓存。
    """
    if not _contacts:
        return jsonify({"error": "通讯录模块未初始化"}), 503

    conn = _db.pool.connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 所有群 ID
        cursor.execute(
            "SELECT DISTINCT roomid FROM messages WHERE roomid IS NOT NULL AND roomid != ''"
        )
        room_ids = [r["roomid"] for r in cursor.fetchall()]

        # 所有用户 ID
        cursor.execute(
            "SELECT DISTINCT sender FROM messages WHERE sender IS NOT NULL AND sender != ''"
        )
        user_ids = [r["sender"] for r in cursor.fetchall()]
    finally:
        conn.close()

    synced_rooms = 0
    synced_users = 0

    # 逐个解析群名（contacts 内部会自动写缓存）
    for rid in room_ids:
        try:
            name = _contacts.get_room_name(rid)
            if name and name != rid:
                synced_rooms += 1
        except Exception:
            pass

    # 逐个解析用户名
    for uid in user_ids:
        try:
            name = _contacts.get_name(uid)
            if name and name != uid:
                synced_users += 1
        except Exception:
            pass

    total = len(room_ids) + len(user_ids)
    synced = synced_rooms + synced_users
    return jsonify({
        "success": True,
        "message": f"同步完成：共 {total} 项，成功解析 {synced} 项（群 {synced_rooms}/{len(room_ids)}，联系人 {synced_users}/{len(user_ids)}）",
        "synced_rooms": synced_rooms,
        "synced_users": synced_users,
        "total_rooms": len(room_ids),
        "total_users": len(user_ids),
    })


@insurance_bp.route("/api/insurance/config/status", methods=["GET"])
def config_status():
    """检查配置状态（OCR、钉钉等关键配置是否就绪）"""
    try:
        config = get_all_insurance_config(_db)

        # 检查百度 OCR 配置
        baidu_ok = all([
            config.get("baidu_ocr_app_id"),
            config.get("baidu_ocr_api_key"),
            config.get("baidu_ocr_secret_key"),
        ])

        # 检查钉钉配置
        dingtalk_ok = all([
            config.get("dingtalk_app_key"),
            config.get("dingtalk_app_secret"),
            config.get("dingtalk_operator_id"),
        ])

        # 钉钉目标表
        targets = config.get("dingtalk_targets", [])
        if isinstance(targets, str):
            try:
                targets = json.loads(targets)
            except Exception:
                targets = []

        status = {
            "baidu_ocr": {
                "configured": baidu_ok,
                "enabled": bool(config.get("baidu_ocr_enabled", True)),
            },
            "dingtalk": {
                "configured": dingtalk_ok,
                "targets_count": len(targets) if isinstance(targets, list) else 0,
            },
            "handler_ready": _handler is not None,
        }
        return jsonify({"code": 0, "data": status})
    except Exception as e:
        logger.exception("获取配置状态失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 字段映射
# ============================================================

@insurance_bp.route("/api/insurance/mapping/config", methods=["GET"])
def get_mapping_config():
    """获取字段映射配置（包含默认映射和各保司映射）"""
    try:
        config = load_mapping_config()
        # 同时返回可用 OCR 字段列表，方便前端构建下拉
        config["available_ocr_fields"] = get_available_ocr_fields()
        return jsonify({"code": 0, "data": config})
    except Exception as e:
        logger.exception("获取字段映射配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/mapping/save", methods=["POST"])
def save_mapping():
    """保存字段映射配置"""
    try:
        body = request.get_json(force=True) or {}
        if not body:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400
        save_mapping_config(body)
        return jsonify({"code": 0, "msg": "映射配置已保存"})
    except Exception as e:
        logger.exception("保存字段映射配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/mapping/company/<company>", methods=["DELETE"])
def delete_company_mapping(company):
    """删除指定保司的字段映射配置"""
    try:
        config = load_mapping_config()
        company_mappings = config.get("company_mappings", {})
        if company not in company_mappings:
            return jsonify({"code": 404, "msg": f"保司 {company} 的映射配置不存在"}), 404
        del company_mappings[company]
        config["company_mappings"] = company_mappings
        save_mapping_config(config)
        return jsonify({"code": 0, "msg": f"已删除保司 {company} 的映射配置"})
    except Exception as e:
        logger.exception("删除保司映射配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/mapping/preview", methods=["POST"])
def preview_mapping():
    """
    预览字段映射效果。
    请求体: {fields: {ocr字段名: 值, ...}, company_short: 保司简称}
    """
    try:
        body = request.get_json(force=True) or {}
        fields = body.get("fields", {})
        company_short = body.get("company_short", "")

        if not fields:
            return jsonify({"code": 400, "msg": "缺少 fields 参数"}), 400

        mapped = apply_mapping(fields, company_short)
        return jsonify({"code": 0, "data": {"mapped_fields": mapped, "output_columns": OUTPUT_COLUMNS}})
    except Exception as e:
        logger.exception("预览字段映射失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 钉钉目标管理
# ============================================================

def _get_dingtalk_targets() -> list:
    """从数据库配置中读取钉钉目标列表"""
    raw = get_insurance_config(_db, "dingtalk_targets", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return raw if isinstance(raw, list) else []


def _save_dingtalk_targets(targets: list):
    """将钉钉目标列表保存到数据库配置"""
    set_insurance_config(_db, "dingtalk_targets", targets)


@insurance_bp.route("/api/insurance/dingtalk/targets", methods=["GET"])
def list_dingtalk_targets():
    """获取钉钉同步目标列表"""
    try:
        targets = _get_dingtalk_targets()
        return jsonify({"code": 0, "data": targets})
    except Exception as e:
        logger.exception("获取钉钉目标列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/dingtalk/targets", methods=["POST"])
def add_dingtalk_target():
    """
    添加钉钉同步目标。
    请求体: {doc_url: 钉钉文档URL, name: 目标名称}
    自动解析 base_id 和 sheet_id。
    """
    try:
        body = request.get_json(force=True) or {}
        doc_url = body.get("doc_url", "").strip()
        name = body.get("name", "").strip()

        if not doc_url:
            return jsonify({"code": 400, "msg": "缺少 doc_url 参数"}), 400

        # 解析钉钉文档 URL 获取 base_id 和 sheet_id
        base_id, sheet_id = parse_dingtalk_doc_url(doc_url)
        if not base_id:
            return jsonify({"code": 400, "msg": "无法从 URL 中解析 base_id，请检查链接格式"}), 400

        # 生成稳定的目标 ID（基于 URL hash）
        target_id = hashlib.md5(doc_url.encode("utf-8")).hexdigest()[:12]

        targets = _get_dingtalk_targets()
        # 检查是否已存在
        for t in targets:
            if t.get("id") == target_id:
                return jsonify({"code": 409, "msg": "该目标已存在"}), 409

        new_target = {
            "id": target_id,
            "name": name or f"目标-{target_id}",
            "doc_url": doc_url,
            "base_id": base_id,
            "sheet_id": sheet_id,
        }
        targets.append(new_target)
        _save_dingtalk_targets(targets)

        return jsonify({"code": 0, "data": new_target, "msg": "目标已添加"})
    except Exception as e:
        logger.exception("添加钉钉目标失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/dingtalk/targets/<target_id>", methods=["PUT"])
def update_dingtalk_target(target_id):
    """更新指定钉钉同步目标"""
    try:
        body = request.get_json(force=True) or {}
        targets = _get_dingtalk_targets()

        found = False
        for t in targets:
            if t.get("id") == target_id:
                # 允许更新名称和 doc_url，更新 URL 时重新解析 base_id/sheet_id
                if "name" in body:
                    t["name"] = body["name"]
                if "doc_url" in body:
                    new_url = body["doc_url"].strip()
                    t["doc_url"] = new_url
                    base_id, sheet_id = parse_dingtalk_doc_url(new_url)
                    t["base_id"] = base_id
                    t["sheet_id"] = sheet_id
                found = True
                break

        if not found:
            return jsonify({"code": 404, "msg": f"目标 {target_id} 不存在"}), 404

        _save_dingtalk_targets(targets)
        return jsonify({"code": 0, "msg": "目标已更新"})
    except Exception as e:
        logger.exception("更新钉钉目标 %s 失败", target_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/dingtalk/targets/<target_id>", methods=["DELETE"])
def delete_dingtalk_target(target_id):
    """删除指定钉钉同步目标"""
    try:
        targets = _get_dingtalk_targets()
        new_targets = [t for t in targets if t.get("id") != target_id]

        if len(new_targets) == len(targets):
            return jsonify({"code": 404, "msg": f"目标 {target_id} 不存在"}), 404

        _save_dingtalk_targets(new_targets)
        return jsonify({"code": 0, "msg": "目标已删除"})
    except Exception as e:
        logger.exception("删除钉钉目标 %s 失败", target_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 同步 & 导出
# ============================================================

@insurance_bp.route("/api/insurance/sync/check-duplicates", methods=["POST"])
def check_duplicates():
    """
    检查保单号是否在钉钉目标表中已存在（去重）。
    请求体: {target_id: str, invoices: [{fields: {...}}, ...], key_field: str}
    """
    try:
        body = request.get_json(force=True) or {}
        target_id = body.get("target_id", "")
        invoices = body.get("invoices", [])
        key_field = body.get("key_field", "保单号")

        if not target_id:
            return jsonify({"code": 400, "msg": "缺少 target_id 参数"}), 400

        # 查找目标配置
        targets = _get_dingtalk_targets()
        target = next((t for t in targets if t.get("id") == target_id), None)
        if not target:
            return jsonify({"code": 404, "msg": f"目标 {target_id} 不存在"}), 404

        # 读取钉钉应用配置
        config = get_all_insurance_config(_db)
        app_key = config.get("dingtalk_app_key", "")
        app_secret = config.get("dingtalk_app_secret", "")
        operator_id = config.get("dingtalk_operator_id", "")

        if not all([app_key, app_secret, operator_id]):
            return jsonify({"code": 400, "msg": "钉钉应用未配置，请先完成钉钉配置"}), 400

        service = DingTalkTableService(
            app_key=app_key,
            app_secret=app_secret,
            base_id=target["base_id"],
            sheet_id=target.get("sheet_id", ""),
            operator_id=operator_id,
        )

        duplicates = service.find_duplicates(invoices, key_field=key_field)
        return jsonify({"code": 0, "data": {"duplicates": duplicates, "count": len(duplicates)}})
    except Exception as e:
        logger.exception("去重检查失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/sync/dingtalk", methods=["POST"])
def batch_sync_dingtalk():
    """
    手动批量同步保单记录到钉钉。
    请求体: {target_id: str, record_ids: [int, ...], field_names: [str, ...]}
    """
    try:
        body = request.get_json(force=True) or {}
        target_id = body.get("target_id", "")
        record_ids = body.get("record_ids", [])
        field_names = body.get("field_names", OUTPUT_COLUMNS)

        if not target_id:
            return jsonify({"code": 400, "msg": "缺少 target_id 参数"}), 400
        if not record_ids:
            return jsonify({"code": 400, "msg": "缺少 record_ids 参数"}), 400

        # 查找目标配置
        targets = _get_dingtalk_targets()
        target = next((t for t in targets if t.get("id") == target_id), None)
        if not target:
            return jsonify({"code": 404, "msg": f"目标 {target_id} 不存在"}), 404

        # 读取钉钉应用配置
        config = get_all_insurance_config(_db)
        app_key = config.get("dingtalk_app_key", "")
        app_secret = config.get("dingtalk_app_secret", "")
        operator_id = config.get("dingtalk_operator_id", "")

        if not all([app_key, app_secret, operator_id]):
            return jsonify({"code": 400, "msg": "钉钉应用未配置，请先完成钉钉配置"}), 400

        service = DingTalkTableService(
            app_key=app_key,
            app_secret=app_secret,
            base_id=target["base_id"],
            sheet_id=target.get("sheet_id", ""),
            operator_id=operator_id,
        )

        # 构建待同步的发票数据
        invoices = []
        success_ids = []
        failed_ids = []
        for rid in record_ids:
            record = get_insurance_record(_db, rid)
            if not record:
                failed_ids.append({"id": rid, "reason": "记录不存在"})
                continue

            mapped = record.get("mapped_fields") or {}
            if isinstance(mapped, str):
                try:
                    mapped = json.loads(mapped)
                except Exception:
                    mapped = {}

            if mapped:
                invoices.append({"fields": mapped, "_record_id": rid})
                success_ids.append(rid)
            else:
                failed_ids.append({"id": rid, "reason": "无映射字段"})

        if not invoices:
            return jsonify({"code": 400, "msg": "没有可同步的记录"}), 400

        # 执行批量插入
        result = service.append_invoices(field_names, invoices)

        # 更新同步状态
        for rid in success_ids:
            try:
                update_insurance_record(_db, rid, {
                    "dingtalk_synced": 1,
                    "dingtalk_target_id": target_id,
                })
            except Exception as upd_err:
                logger.warning("更新记录 %d 同步状态失败: %s", rid, upd_err)

        return jsonify({
            "code": 0,
            "data": {
                "synced": len(success_ids),
                "failed": failed_ids,
                "detail": result,
            },
            "msg": f"成功同步 {len(success_ids)} 条记录",
        })
    except Exception as e:
        logger.exception("批量同步钉钉失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/export/excel", methods=["POST"])
def export_excel():
    """
    导出保单数据为 Excel 文件。
    请求体: {invoices: [...], field_names: [...], export_type: "success"|"failed", sheet_name: str}
    """
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({"code": 500, "msg": "缺少 openpyxl 依赖，请安装: pip install openpyxl"}), 500

    try:
        body = request.get_json(force=True) or {}
        invoices = body.get("invoices", [])
        field_names = body.get("field_names", OUTPUT_COLUMNS)
        export_type = body.get("export_type", "success")  # "success" 或 "failed"
        sheet_name = body.get("sheet_name", "保单数据")

        if not invoices:
            return jsonify({"code": 400, "msg": "没有可导出的数据"}), 400

        # 创建工作簿
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name[:31]  # Excel sheet 名最长 31 字符

        if export_type == "failed":
            # 失败类型：文件名 + 失败原因 两列
            headers = ["文件名", "失败原因"]
            ws.append(headers)
            for inv in invoices:
                fields = inv.get("fields", inv) if isinstance(inv, dict) else {}
                ws.append([
                    inv.get("file_name", inv.get("filename", fields.get("文件名", ""))),
                    inv.get("error", inv.get("error_message", fields.get("失败原因", ""))),
                ])
        else:
            # 成功类型：文件名 + 各字段列
            headers = ["文件名"] + list(field_names)
            ws.append(headers)
            for inv in invoices:
                fields = inv.get("fields", {}) if isinstance(inv, dict) else {}
                row = [inv.get("file_name", inv.get("filename", fields.get("文件名", "")))]
                for col in field_names:
                    row.append(fields.get(col, ""))
                ws.append(row)

        # 自动列宽（最大 40 字符）
        for col_idx, _ in enumerate(ws[1], start=1):
            col_letter = get_column_letter(col_idx)
            max_len = 0
            for cell in ws[col_letter]:
                try:
                    cell_len = len(str(cell.value)) if cell.value else 0
                    max_len = max(max_len, cell_len)
                except Exception:
                    pass
            adjusted_width = min(max_len + 2, 40)
            ws.column_dimensions[col_letter].width = adjusted_width

        # 保存到内存流
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        download_name = f"保单导出_{sheet_name}.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_name,
        )
    except Exception as e:
        logger.exception("导出 Excel 失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/extraction-rules", methods=["GET"])
def get_extraction_rules_api():
    """获取保单字段提取规则（供前端展示）"""
    try:
        rules = get_extraction_rules()
        return jsonify({"code": 0, "data": rules})
    except Exception as e:
        logger.exception("获取字段提取规则失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 模板列表
# ============================================================

@insurance_bp.route('/api/insurance/templates', methods=['GET'])
def list_templates():
    """获取已注册的模板列表"""
    import os
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    registry_path = os.path.join(templates_dir, "registry.json")
    if not os.path.exists(registry_path):
        return jsonify({"success": True, "data": []})
    with open(registry_path, "r", encoding="utf-8") as f:
        registry = json.load(f)
    # registry 结构为 {template_id: {info...}, ...}，转为列表返回
    result = []
    for template_id, info in registry.items():
        result.append({
            "template_id": template_id,
            **info,
        })
    return jsonify({"success": True, "data": result})


@insurance_bp.route('/api/insurance/train_records', methods=['GET'])
def list_train_records():
    """获取训练记录列表"""
    if not _db:
        return jsonify({"success": False, "error": "数据库未初始化"})
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    conn = _db.pool.connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM template_train_records")
        total = cursor.fetchone()["cnt"]
        offset = (page - 1) * page_size
        cursor.execute("""
            SELECT id, company_id, policy_type, version, sample_count,
                   discovered_fields, overall_hit_rate, passed, unstable_fields,
                   registered, trigger_source, created_at
            FROM template_train_records
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (page_size, offset))
        records = cursor.fetchall()
        # 转换 datetime 为字符串
        for r in records:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
        return jsonify({"success": True, "data": {"records": records, "total": total}})
    finally:
        conn.close()


# ============================================================
# 模板训练
# ============================================================

@insurance_bp.route('/api/insurance/train', methods=['POST'])
def train_template():
    """上传样本 PDF，自动训练模板"""
    from insurance.learner.trainer import TemplateTrainer, save_training_result
    import os

    files = request.files.getlist('samples')
    company_id = request.form.get('company_id', '')
    policy_type = request.form.get('policy_type', '')
    version = request.form.get('version', 'v1')
    company_name = request.form.get('company_name', '')
    company_short = request.form.get('company_short', '')

    if not files or len(files) < 2:
        return jsonify({"success": False, "error": "至少需要上传 2 份样本 PDF"})
    if not company_id or not policy_type:
        return jsonify({"success": False, "error": "必须指定 company_id 和 policy_type"})

    pdf_bytes_list = [f.read() for f in files]

    trainer = TemplateTrainer()
    result = trainer.train(
        pdf_bytes_list, company_id, policy_type, version)

    # 保存训练记录到数据库（不会触发 reloader）
    if _db:
        from insurance.db import save_train_record
        save_train_record(_db, {
            "company_id": company_id,
            "policy_type": policy_type,
            "version": version,
            "sample_count": len(pdf_bytes_list),
            "discovered_fields": len(result.fields) if result.fields else 0,
            "overall_hit_rate": result.validation.overall_hit_rate if result.validation else 0,
            "passed": 1 if result.success else 0,
            "unstable_fields": result.needs_review,
            "template_json": json.dumps(result.template, ensure_ascii=False) if result.template else "",
            "rule_code": result.rule_code,
            "registered": 1 if result.success else 0,
            "trigger_source": "manual",
        })

    # 延迟保存模板和规则文件到磁盘，避免写入 .py 文件触发 Flask reloader 导致响应中断
    if result.success:
        import threading
        base_dir = os.path.dirname(os.path.abspath(__file__))
        templates_dir = os.path.join(base_dir, "templates")
        rules_dir = os.path.join(base_dir, "rules")

        def _deferred_save():
            import time
            time.sleep(1)  # 等响应发送完毕
            save_training_result(templates_dir, rules_dir, result,
                               company_id, policy_type, version)

        threading.Thread(target=_deferred_save, daemon=True).start()

    return jsonify({
        "success": result.success,
        "error": result.error,
        "template_id": f"{company_id}/{policy_type}/{version}" if result.success else "",
        "validation": {
            "hit_rate": result.validation.overall_hit_rate if result.validation else 0,
            "sample_count": result.validation.sample_count if result.validation else 0,
            "passed": result.validation.passed if result.validation else False,
            "unstable_fields": result.needs_review or [],
        }
    })


# ============================================================
# 告警管理
# ============================================================

@insurance_bp.route('/api/insurance/alerts', methods=['GET'])
def list_alerts():
    """查询告警记录"""
    from insurance.db import query_alerts
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    resolved = request.args.get('resolved', None)
    level = request.args.get('level', None)
    if resolved is not None:
        resolved = int(resolved)
    result = query_alerts(_db, page, page_size, resolved, level)
    return jsonify({"success": True, "data": result})


@insurance_bp.route('/api/insurance/alerts/<int:alert_id>/resolve', methods=['POST'])
def resolve_alert_api(alert_id):
    """标记告警已处理"""
    from insurance.db import resolve_alert
    data = request.get_json(silent=True) or {}
    action = data.get('action', 'ignore')
    resolve_alert(_db, alert_id, action)
    return jsonify({"success": True})


# ============================================================
# 监控配置管理
# ============================================================

def _add_id_fields(config):
    """将 rooms/users 对象数组转为前端期望的 room_ids/user_ids 纯ID数组"""
    rooms = config.get("rooms") or []
    users = config.get("users") or []
    # rooms 可能是 [{"id":"wr...","name":"群名"}] 或 ["wr..."]
    config["room_ids"] = [
        r["id"] if isinstance(r, dict) else r for r in rooms
    ]
    config["user_ids"] = [
        u["id"] if isinstance(u, dict) else u for u in users
    ]


@insurance_bp.route("/api/monitor-configs", methods=["GET"])
def list_monitor_configs_api():
    """获取全部监控配置"""
    try:
        configs = list_monitor_configs(_db)
        for c in configs:
            for ts_field in ("created_at", "updated_at"):
                if c.get(ts_field) and not isinstance(c[ts_field], str):
                    c[ts_field] = str(c[ts_field])
            # 前端期望 room_ids/user_ids（纯ID数组）
            _add_id_fields(c)
        return jsonify({"code": 0, "data": configs})
    except Exception as e:
        logger.exception("获取监控配置列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs", methods=["POST"])
def create_monitor_config_api():
    """新增监控配置"""
    try:
        body = request.get_json(force=True) or {}
        name = body.get("name", "").strip()
        if not name:
            return jsonify({"code": 400, "msg": "监控名称不能为空"}), 400

        doc_url = body.get("dingtalk_doc_url", "").strip()
        base_id, sheet_id = "", ""
        if doc_url:
            base_id, sheet_id = parse_dingtalk_doc_url(doc_url)

        config_id = create_monitor_config(_db, {
            "name": name,
            "enabled": 1 if body.get("enabled", True) else 0,
            "rooms": body.get("room_ids", body.get("rooms", [])),
            "users": body.get("user_ids", body.get("users", [])),
            "dingtalk_doc_url": doc_url,
            "dingtalk_base_id": base_id,
            "dingtalk_sheet_id": sheet_id,
            "field_mapping": body.get("field_mapping", {}),
        })

        config = get_monitor_config(_db, config_id)
        _add_id_fields(config)
        return jsonify({"code": 0, "data": config, "msg": "监控配置已创建"})
    except Exception as e:
        logger.exception("创建监控配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>", methods=["PUT"])
def update_monitor_config_api(config_id):
    """更新监控配置"""
    try:
        body = request.get_json(force=True) or {}
        if not body:
            return jsonify({"code": 400, "msg": "请求体不能为空"}), 400

        updates = {}
        if "name" in body:
            updates["name"] = body["name"].strip()
        if "enabled" in body:
            updates["enabled"] = 1 if body["enabled"] else 0
        if "room_ids" in body or "rooms" in body:
            updates["rooms"] = body.get("room_ids", body.get("rooms"))
        if "user_ids" in body or "users" in body:
            updates["users"] = body.get("user_ids", body.get("users"))
        if "field_mapping" in body:
            updates["field_mapping"] = body["field_mapping"]
        if "dingtalk_doc_url" in body:
            doc_url = body["dingtalk_doc_url"].strip()
            updates["dingtalk_doc_url"] = doc_url
            if doc_url:
                base_id, sheet_id = parse_dingtalk_doc_url(doc_url)
                updates["dingtalk_base_id"] = base_id
                updates["dingtalk_sheet_id"] = sheet_id
            else:
                updates["dingtalk_base_id"] = ""
                updates["dingtalk_sheet_id"] = ""

        ok = update_monitor_config(_db, config_id, updates)
        if not ok:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404

        config = get_monitor_config(_db, config_id)
        _add_id_fields(config)
        return jsonify({"code": 0, "data": config, "msg": "监控配置已更新"})
    except Exception as e:
        logger.exception("更新监控配置 %d 失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>", methods=["DELETE"])
def delete_monitor_config_api(config_id):
    """删除监控配置"""
    try:
        ok = delete_monitor_config(_db, config_id)
        if not ok:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404
        return jsonify({"code": 0, "msg": "监控配置已删除"})
    except Exception as e:
        logger.exception("删除监控配置 %d 失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/monitor-configs/<int:config_id>/toggle", methods=["PUT"])
def toggle_monitor_config_api(config_id):
    """启停监控配置"""
    try:
        config = get_monitor_config(_db, config_id)
        if not config:
            return jsonify({"code": 404, "msg": "监控配置不存在"}), 404

        new_enabled = 0 if config["enabled"] else 1
        update_monitor_config(_db, config_id, {"enabled": new_enabled})
        return jsonify({"code": 0, "data": {"enabled": bool(new_enabled)}, "msg": "已更新"})
    except Exception as e:
        logger.exception("切换监控配置 %d 状态失败", config_id)
        return jsonify({"code": 500, "msg": str(e)}), 500
