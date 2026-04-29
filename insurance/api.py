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
    get_company_stats,
    get_insurance_config,
    get_insurance_record,
    get_insurance_stats,
    query_insurance_records,
    save_insurance_record,
    set_insurance_config,
    update_insurance_record,
    upsert_insurance_record_by_policy,
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
from .policy_parser import get_extraction_rules, parse_policy_text, parse_policy_text_multi
from .ocr_service import extract_text_from_pdf
from auth.decorators import login_required
from auth.db import ROLE_SUPER_ADMIN, ROLE_ENTERPRISE, get_enterprise_employee_ids

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
# 全局鉴权：所有保单识别 API 均需登录
# ============================================================

@insurance_bp.before_request
def _require_login():
    """保单识别 API 统一鉴权"""
    from auth.jwt_utils import verify_token
    from auth.db import get_user_by_id

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.args.get("token")
    if not token:
        return jsonify({"code": 401, "msg": "未登录"}), 401

    payload = verify_token(token)
    if not payload:
        return jsonify({"code": 401, "msg": "登录已过期，请重新登录"}), 401

    user = get_user_by_id(_db, payload["user_id"])
    if not user or not user.get("enabled"):
        return jsonify({"code": 401, "msg": "账号已禁用"}), 401

    from flask import g
    g.current_user = {
        "user_id": payload["user_id"],
        "phone": payload.get("phone", payload.get("username", "")),
        "role": payload["role"],
        "parent_id": user.get("parent_id"),
    }


def _get_user_ids_filter():
    """根据当前用户角色返回 user_ids 过滤列表，超管返回 None（不过滤）"""
    from flask import g
    role = g.current_user["role"]
    uid = g.current_user["user_id"]
    if role == ROLE_SUPER_ADMIN:
        return None
    elif role == ROLE_ENTERPRISE:
        return get_enterprise_employee_ids(_db, uid)
    else:
        return [uid]


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
    source_type = request.args.get("source_type", "")
    sender = request.args.get("sender", "")
    ocr_engine = request.args.get("ocr_engine", "")
    doc_category = request.args.get("doc_category", "")
    is_abnormal = request.args.get("is_abnormal", "")
    company_short = request.args.get("company_short", "")
    date_start = request.args.get("date_start", "")
    date_end = request.args.get("date_end", "")
    # 关联表模糊搜索
    search_company = request.args.get("search_company", "")
    search_policy_no = request.args.get("search_policy_no", "")
    search_plate_no = request.args.get("search_plate_no", "")
    search_applicant = request.args.get("search_applicant", "")
    search_insured = request.args.get("search_insured", "")
    search_salesperson = request.args.get("search_salesperson", "")
    # 关联表日期筛选
    sign_date_start = request.args.get("sign_date_start", "")
    sign_date_end = request.args.get("sign_date_end", "")
    start_date_start = request.args.get("start_date_start", "")
    start_date_end = request.args.get("start_date_end", "")
    end_date_start = request.args.get("end_date_start", "")
    end_date_end = request.args.get("end_date_end", "")

    try:
        result = query_insurance_records(
            _db,
            page=page,
            page_size=page_size,
            roomid=roomid,
            status=status,
            keyword=keyword,
            source=source,
            source_type=source_type,
            sender=sender,
            ocr_engine=ocr_engine,
            doc_category=doc_category,
            is_abnormal=is_abnormal,
            company_short=company_short,
            date_start=date_start,
            date_end=date_end,
            user_ids=_get_user_ids_filter(),
            search_company=search_company,
            search_policy_no=search_policy_no,
            search_plate_no=search_plate_no,
            search_applicant=search_applicant,
            search_insured=search_insured,
            search_salesperson=search_salesperson,
            sign_date_start=sign_date_start,
            sign_date_end=sign_date_end,
            start_date_start=start_date_start,
            start_date_end=start_date_end,
            end_date_start=end_date_start,
            end_date_end=end_date_end,
        )
        # 列表返回 display_fields（轻量映射字段）替代 parsed_fields
        for record in result.get("records", []):
            # 反序列化 display_fields
            if isinstance(record.get("display_fields"), str):
                try:
                    record["display_fields"] = json.loads(record["display_fields"])
                except (TypeError, json.JSONDecodeError):
                    record["display_fields"] = {}
            # 时间戳转字符串
            for ts_field in ("created_at",):
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
        for field in ("parsed_fields", "mapped_fields", "manual_fields"):
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


@insurance_bp.route("/api/insurance/records/<int:record_id>/fields", methods=["PUT"])
def update_record_fields(record_id):
    """手动修改保单识别字段值，并记录哪些字段被手动修改过"""
    _require_login()
    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        data = request.get_json(force=True)
        updated_fields = data.get("fields", {})  # {字段名: 新值}
        if not updated_fields:
            return jsonify({"code": 400, "msg": "未提供修改字段"}), 400

        # 读取现有 parsed_fields
        pf = record.get("parsed_fields") or "{}"
        if isinstance(pf, str):
            try:
                pf = json.loads(pf)
            except (TypeError, json.JSONDecodeError):
                pf = {}

        # 读取现有 manual_fields（手动修改过的字段名列表）
        mf = record.get("manual_fields") or "[]"
        if isinstance(mf, str):
            try:
                mf = json.loads(mf)
            except (TypeError, json.JSONDecodeError):
                mf = []
        if not isinstance(mf, list):
            mf = []

        # 合并修改
        for field_name, new_value in updated_fields.items():
            pf[field_name] = new_value
            if field_name not in mf:
                mf.append(field_name)

        # 更新数据库
        update_insurance_record(_db, record_id, {
            "parsed_fields": pf,
            "manual_fields": mf,
        })

        return jsonify({"code": 0, "msg": "保存成功", "data": {
            "parsed_fields": pf,
            "manual_fields": mf,
        }})
    except Exception as e:
        logger.exception("更新保单字段 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/records/<int:record_id>/preview", methods=["GET"])
def preview_record_file(record_id):
    """代理预览保单文件（避免 COS 直接下载）"""
    import requests as http_requests

    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        cos_url = record.get("cos_url", "")
        if not cos_url:
            return jsonify({"code": 404, "msg": "文件 URL 不存在"}), 404

        # 请求 COS 文件
        resp = http_requests.get(cos_url, timeout=30, stream=True)
        if resp.status_code != 200:
            return jsonify({"code": 502, "msg": "文件下载失败"}), 502

        filename = record.get("filename", "file.pdf")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"
        mime_map = {
            "pdf": "application/pdf",
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "bmp": "image/bmp",
        }
        mimetype = mime_map.get(ext, "application/octet-stream")

        return send_file(
            io.BytesIO(resp.content),
            mimetype=mimetype,
            as_attachment=False,
            download_name=filename,
        )
    except Exception as e:
        logger.exception("预览保单文件 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/records/<int:record_id>/raw-text", methods=["GET"])
def get_record_raw_text(record_id):
    """获取保单记录的 OCR 原文（优先从数据库缓存读取，否则从 COS 提取并缓存）"""
    import requests as http_requests

    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        # 优先从数据库缓存读取
        cached_text = record.get("raw_text", "")
        if cached_text:
            return jsonify({"code": 0, "data": {"raw_text": cached_text}})

        # 数据库无缓存，从 COS 下载提取
        cos_url = record.get("cos_url", "")
        if not cos_url:
            return jsonify({"code": 404, "msg": "文件 URL 不存在"}), 404

        resp = http_requests.get(cos_url, timeout=30)
        if resp.status_code != 200:
            return jsonify({"code": 502, "msg": "文件下载失败"}), 502

        text = ""
        filename = record.get("filename", "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"

        if ext == "pdf":
            try:
                from insurance.ocr_service import extract_text_from_pdf_bytes
                result = extract_text_from_pdf_bytes(resp.content)
                if result.get("success"):
                    text = result.get("text", "")
            except Exception as e:
                logger.warning("pdfplumber 提取原文失败: %s", e)

        # 写回数据库缓存
        if text:
            try:
                update_insurance_record(_db, record_id, {"raw_text": text})
            except Exception:
                pass

        return jsonify({"code": 0, "data": {"raw_text": text}})
    except Exception as e:
        logger.exception("获取保单原文 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/stats", methods=["GET"])
def get_stats():
    """获取保单识别统计信息，支持与列表相同的筛选参数"""
    try:
        filters = {}
        for key in ("roomid", "source_type", "sender", "keyword",
                     "company_short", "ocr_engine", "date_start", "date_end",
                     "search_company", "search_policy_no", "search_plate_no",
                     "search_applicant", "search_insured", "search_salesperson",
                     "sign_date_start", "sign_date_end",
                     "start_date_start", "start_date_end",
                     "end_date_start", "end_date_end"):
            val = request.args.get(key, "").strip()
            if val:
                filters[key] = val
        # 按账号权限过滤
        user_ids = _get_user_ids_filter()
        if user_ids is not None:
            filters["user_ids"] = user_ids
        stats = get_insurance_stats(_db, filters=filters if filters else None)
        return jsonify({"code": 0, "data": stats})
    except Exception as e:
        logger.exception("获取保单统计失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# 支持保司统计缓存（后端5分钟缓存，避免频繁查库）
_company_cache = {"data": None, "time": 0}
_COMPANY_CACHE_TTL = 300  # 5分钟


@insurance_bp.route("/api/insurance/supported-companies", methods=["GET"])
def get_supported_companies():
    """
    获取支持的保司列表（从已识别记录中统计，5分钟缓存）。
    返回: {companies: [{company, count, supported}], threshold, aliases: {原名: 简称}}
    """
    import time as _time

    # 检查缓存
    if _company_cache["data"] and (_time.time() - _company_cache["time"] < _COMPANY_CACHE_TTL):
        return jsonify({"code": 0, "data": _company_cache["data"]})

    try:
        # 从配置读取阈值和别名映射
        threshold = get_insurance_config(_db, "company_threshold", 10)
        if isinstance(threshold, str):
            try:
                threshold = int(threshold)
            except ValueError:
                threshold = 10
        aliases = get_insurance_config(_db, "company_aliases", {})
        if not isinstance(aliases, dict):
            aliases = {}

        # 从数据库统计各保司识别数量
        stats = get_company_stats(_db)

        # 应用别名合并统计：多个原名 → 同一简称的数量合并
        merged = {}
        for item in stats:
            company = item["company"]
            display_name = aliases.get(company, company)
            if display_name in merged:
                merged[display_name]["count"] += item["count"]
            else:
                merged[display_name] = {"company": display_name, "count": item["count"]}

        companies = sorted(merged.values(), key=lambda x: x["count"], reverse=True)
        for c in companies:
            c["supported"] = c["count"] >= threshold

        result_data = {
            "companies": companies,
            "threshold": threshold,
            "aliases": aliases,
        }
        _company_cache["data"] = result_data
        _company_cache["time"] = _time.time()

        return jsonify({"code": 0, "data": result_data})
    except Exception as e:
        logger.exception("获取支持保司列表失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/supported-companies/config", methods=["PUT"])
def update_supported_companies_config():
    """
    更新保司支持配置。
    请求体: {threshold?: int, aliases?: {原名: 简称, ...}}
    """
    try:
        body = request.get_json(force=True) or {}

        if "threshold" in body:
            set_insurance_config(_db, "company_threshold", int(body["threshold"]))
        if "aliases" in body:
            if not isinstance(body["aliases"], dict):
                return jsonify({"code": 400, "msg": "aliases 必须是对象格式"}), 400
            set_insurance_config(_db, "company_aliases", body["aliases"])

        # 清除缓存
        _company_cache["data"] = None
        _company_cache["time"] = 0

        return jsonify({"code": 0, "msg": "配置已更新"})
    except Exception as e:
        logger.exception("更新保司配置失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 手动上传识别
# ============================================================

def _save_manual_record(file_name, policy, ocr_engine, file_data_b64=None, upload_cos=False):
    """
    保存手动识别记录到数据库（按保单号去重：存在则更新，否则新建）。
    upload_cos=True 时上传文件到 COS。
    返回 (record_id, is_update)。
    """
    parsed_fields = policy.get("fields", {})
    doc_category = policy.get("doc_category", "")
    confidence = policy.get("confidence", 0.0)

    # 字段映射
    company_short = parsed_fields.get("保险公司简称", "")
    mapped_fields = apply_mapping(parsed_fields, company_short)

    cos_url = ""
    # 上传 COS（仅在开关打开且有文件数据时）
    if upload_cos and file_data_b64 and _handler and getattr(_handler, "cos_storage", None):
        try:
            import os
            import time
            pdf_bytes = base64.b64decode(file_data_b64)
            name, ext = os.path.splitext(file_name)
            key_suffix = f"insurance/manual/{file_name}"
            if _handler.cos_storage.file_exists(key_suffix):
                ts = int(time.time() * 1000)
                key_suffix = f"insurance/manual/{name}_{ts}{ext}"
            cos_url = _handler.cos_storage.upload_bytes(pdf_bytes, key_suffix) or ""
        except Exception as e:
            logger.warning("手动上传 COS 失败: %s", e)

    # 计算文件 MD5（用于去重）
    file_md5 = None
    if file_data_b64:
        try:
            raw_bytes = base64.b64decode(file_data_b64)
            file_md5 = hashlib.md5(raw_bytes).hexdigest()
        except Exception:
            pass

    # 关联当前登录用户
    from flask import g
    current_user_id = g.current_user["user_id"] if hasattr(g, "current_user") else None

    record = {
        "filename": file_name,
        "cos_url": cos_url,
        "ocr_engine": ocr_engine,
        "doc_category": doc_category,
        "confidence": confidence,
        "parsed_fields": parsed_fields,
        "mapped_fields": mapped_fields,
        "status": "done",
        "source": "manual",
        "user_id": current_user_id,
    }
    if file_md5:
        record["file_md5"] = file_md5

    try:
        record_id, is_update = upsert_insurance_record_by_policy(_db, record)
        logger.info("手动识别记录已%s, id=%d, file=%s",
                     "更新" if is_update else "新建", record_id, file_name)
        return record_id, is_update
    except Exception as e:
        logger.warning("手动识别记录保存失败: %s", e)
        return None, False


@insurance_bp.route("/api/insurance/parse", methods=["POST"])
def parse_text_only():
    """
    纯文本解析（不做 OCR），接收前端提取的文本直接解析保单字段。
    请求体: {text, file_name, upload_cos, file_data(可选，upload_cos=true时用于上传COS)}
    """
    try:
        body = request.get_json(force=True) or {}
        text = body.get("text", "")
        file_name = body.get("file_name", "upload")
        upload_cos = body.get("upload_cos", False)
        file_data_b64 = body.get("file_data", "")

        if not text:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "文本为空", "invoices": [],
            })

        from .ocr_service import clean_text
        text = clean_text(text)
        policies = parse_policy_text_multi(text)

        # 过滤无效保单
        valid_policies = [p for p in policies if p.get("fields") and p.get("confidence", 0) > 0]
        if not valid_policies:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "need_ocr", "invoices": [],
            })

        # 逐条保存识别记录到数据库
        record_ids = []
        is_updates = []
        for policy in valid_policies:
            rid, is_upd = _save_manual_record(
                file_name, policy, "pdfjs",
                file_data_b64=file_data_b64, upload_cos=upload_cos)
            record_ids.append(rid)
            is_updates.append(is_upd)

        return jsonify({
            "success": True,
            "file_name": file_name,
            "invoices": valid_policies,
            "char_count": len(text),
            "ocr_engine": "pdfjs",
            "record_id": record_ids[0] if record_ids else None,
            "record_ids": record_ids,
            "is_update": is_updates[0] if is_updates else False,
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
    手动上传文件进行保单识别，识别结果保存到数据库（按保单号去重）。
    upload_cos=true 时同时上传文件到 COS。
    请求体: {file_data(base64), file_type, file_name, pdf_page, upload_cos}
    """
    try:
        body = request.get_json(force=True) or {}
        file_data_b64 = body.get("file_data", "")
        file_type = body.get("file_type", "pdf")
        file_name = body.get("file_name", "upload")
        pdf_page = body.get("pdf_page", 0) or 0  # 0=提取所有页
        upload_cos = body.get("upload_cos", False)

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

            # pdfplumber 失败（无文本层）→ 降级到百度 OCR（多页识别）
            if not extract_result["success"] and baidu_ocr_client:
                logger.info("[%s] pdfplumber 无文本层，降级使用百度OCR（多页）", file_name)
                pdf_bytes = base64.b64decode(file_data_b64)
                extract_result = baidu_ocr_client.recognize_pdf_multi_pages(
                    pdf_bytes, max_pages=6)
                ocr_engine = "baidu"

        if not extract_result["success"]:
            error_msg = extract_result.get("error", "OCR 识别失败")
            if "无文本层" in error_msg and not baidu_ocr_client:
                error_msg += "（提示：配置百度OCR可自动识别扫描件）"
            return jsonify({
                "success": False, "file_name": file_name,
                "error": error_msg, "invoices": [],
            })

        policies = parse_policy_text_multi(extract_result["text"])

        # 质量不佳时降级百度 OCR（用第一条保单判断质量）
        first_policy = policies[0] if policies else {}
        need_fallback = (
            not first_policy.get("fields")
            or first_policy.get("doc_category") == "其他"
            or first_policy.get("confidence", 0) == 0
        )
        if need_fallback and ocr_engine == "pdfplumber" and baidu_ocr_client:
            logger.info("[%s] pdfplumber 效果不佳(类型=%s, 置信度=%s), 降级百度OCR（多页）",
                        file_name, first_policy.get("doc_category"), first_policy.get("confidence"))
            pdf_bytes = base64.b64decode(file_data_b64)
            extract_result = baidu_ocr_client.recognize_pdf_multi_pages(
                pdf_bytes, max_pages=6)
            ocr_engine = "baidu"
            if extract_result["success"]:
                policies = parse_policy_text_multi(extract_result["text"])

        # 过滤无效保单
        valid_policies = [p for p in policies if p.get("fields") and p.get("confidence", 0) > 0]
        if not valid_policies:
            return jsonify({
                "success": False, "file_name": file_name,
                "error": "未识别到任何保单关键字段", "invoices": [],
            })

        # 逐条处理：同车牌互补 + 保存记录
        record_ids = []
        is_updates = []
        for policy in valid_policies:
            if _handler and policy.get("fields"):
                _handler._cross_fill_by_plate(policy["fields"])
            rid, is_upd = _save_manual_record(
                file_name, policy, ocr_engine,
                file_data_b64=file_data_b64, upload_cos=upload_cos)
            record_ids.append(rid)
            is_updates.append(is_upd)

        return jsonify({
            "success": True,
            "file_name": file_name,
            "invoices": valid_policies,
            "char_count": extract_result.get("char_count", 0),
            "ocr_engine": ocr_engine,
            "record_id": record_ids[0] if record_ids else None,
            "record_ids": record_ids,
            "is_update": is_updates[0] if is_updates else False,
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


@insurance_bp.route("/api/insurance/reocr/<int:record_id>", methods=["POST"])
def reocr_record(record_id):
    """
    重新识别：从 COS 下载文件重新 OCR，更新记录中的识别结果。
    不会自动同步钉钉，需要用户预览确认后手动触发同步。
    """
    import requests as http_requests

    if not _handler:
        return jsonify({"code": 503, "msg": "识别服务未初始化"}), 503

    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        cos_url = record.get("cos_url", "")
        if not cos_url:
            return jsonify({"code": 400, "msg": "记录无文件 URL，无法重新识别"}), 400

        # 1. 从 COS 下载 PDF
        resp = http_requests.get(cos_url, timeout=60)
        if resp.status_code != 200:
            return jsonify({"code": 502, "msg": "文件下载失败"}), 502
        pdf_bytes = resp.content

        # 2. 重新 OCR
        filename = record.get("filename", "file.pdf")
        ocr_result = _handler._do_ocr(pdf_bytes, filename)
        if not ocr_result.get("success"):
            return jsonify({"code": 500, "msg": f"OCR 识别失败: {ocr_result.get('error', '未知错误')}"}), 500

        policies = ocr_result.get("policies", [])
        ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")

        if not policies:
            return jsonify({"code": 500, "msg": "OCR 未解析到任何保单"}), 500

        from insurance.field_mapping import apply_mapping

        # 逐条处理：第1条更新原记录，后续创建新记录
        extra_record_ids = []
        for idx, policy in enumerate(policies):
            parsed_fields = policy.get("fields", {})
            doc_category = policy.get("doc_category", "")
            confidence = policy.get("confidence", 0.0)

            cur_record_id = record_id
            if idx > 0:
                # 多保单：为第2条及之后创建新记录
                new_record = {
                    "msg_seq": record.get("msg_seq"),
                    "roomid": record.get("roomid", ""),
                    "room_name": record.get("room_name", ""),
                    "sender": record.get("sender", ""),
                    "sender_name": record.get("sender_name", ""),
                    "filename": filename,
                    "filesize": record.get("filesize", 0),
                    "status": "processing",
                    "source": record.get("source", "auto"),
                    "user_id": record.get("user_id"),
                    "file_md5": record.get("file_md5"),
                    "cos_url": cos_url,
                }
                cur_record_id = save_insurance_record(_db, new_record)
                extra_record_ids.append(cur_record_id)

            # 同车牌保单字段互补
            _handler._cross_fill_by_plate(parsed_fields, cur_record_id)

            # 字段映射
            company_short = parsed_fields.get("保险公司简称", "")
            mapped_fields = apply_mapping(parsed_fields, company_short)

            # 更新记录
            raw_text = policy.get("raw_text", "")
            updates = {
                "status": "done",
                "ocr_engine": ocr_engine,
                "doc_category": doc_category,
                "confidence": confidence,
                "parsed_fields": parsed_fields,
                "mapped_fields": mapped_fields,
                "raw_text": raw_text,
                "error_message": "",
                "cos_url": cos_url,
            }
            update_insurance_record(_db, cur_record_id, updates)

        # 返回更新后的主记录
        updated = get_insurance_record(_db, record_id)
        for field in ("parsed_fields", "mapped_fields"):
            if isinstance(updated.get(field), str):
                try:
                    updated[field] = json.loads(updated[field])
                except (TypeError, json.JSONDecodeError):
                    pass
        for ts_field in ("created_at", "updated_at"):
            if updated.get(ts_field) and not isinstance(updated[ts_field], str):
                updated[ts_field] = str(updated[ts_field])

        msg = "重新识别完成，请预览确认后同步钉钉"
        if extra_record_ids:
            msg = f"重新识别完成，检测到{len(policies)}份保单，已创建{len(extra_record_ids)}条新记录"

        return jsonify({
            "code": 0,
            "data": updated,
            "msg": msg,
            "extra_record_ids": extra_record_ids,
        })
    except Exception as e:
        logger.exception("重新识别保单记录 %d 失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/sync/<int:record_id>", methods=["POST"])
def sync_record_to_dingtalk(record_id):
    """手动触发单条记录同步到钉钉（支持覆盖已同步的记录）"""
    if not _handler:
        return jsonify({"code": 503, "msg": "识别服务未初始化"}), 503

    try:
        record = get_insurance_record(_db, record_id)
        if not record:
            return jsonify({"code": 404, "msg": "记录不存在"}), 404

        # 重置同步状态，允许重新同步
        if record.get("dingtalk_synced"):
            update_insurance_record(_db, record_id, {"dingtalk_synced": 0})

        # 解析字段
        parsed_fields = record.get("parsed_fields") or {}
        if isinstance(parsed_fields, str):
            try:
                parsed_fields = json.loads(parsed_fields)
            except (TypeError, json.JSONDecodeError):
                parsed_fields = {}
        mapped_fields = record.get("mapped_fields") or {}
        if isinstance(mapped_fields, str):
            try:
                mapped_fields = json.loads(mapped_fields)
            except (TypeError, json.JSONDecodeError):
                mapped_fields = {}

        # 如果没有 mapped_fields，重新生成
        if not mapped_fields and parsed_fields:
            company_short = parsed_fields.get("保险公司简称", "")
            mapped_fields = apply_mapping(parsed_fields, company_short)

        roomid = record.get("roomid", "")
        sender = record.get("sender", "")
        monitors = _handler._get_matched_monitors(roomid, sender)
        if not monitors:
            return jsonify({"code": 400, "msg": "该记录无匹配的监控配置，无法确定同步目标"}), 400

        synced = False
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
            _handler._sync_to_dingtalk_v2(
                record_id, sync_fields,
                sender_name=record.get("sender_name", ""),
                cos_url=record.get("cos_url", ""),
                doc_category=record.get("doc_category", ""),
                monitor=monitor,
            )
            synced = True

        if synced:
            update_insurance_record(_db, record_id, {"dingtalk_synced": 1})
            return jsonify({"code": 0, "msg": "同步成功"})
        else:
            return jsonify({"code": 400, "msg": "监控配置未绑定钉钉文档"}), 400
    except Exception as e:
        logger.exception("同步记录 %d 到钉钉失败", record_id)
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/batch-reocr-sync", methods=["POST"])
def batch_reocr_sync():
    """
    批量重新识别并同步钉钉。
    对选中的记录逐条执行：重新OCR → 同步到钉钉。
    请求体: {record_ids: [int, ...]}
    """
    import requests as http_requests

    if not _handler:
        return jsonify({"code": 503, "msg": "识别服务未初始化"}), 503

    try:
        body = request.get_json(force=True) or {}
        record_ids = body.get("record_ids", [])

        if not record_ids:
            return jsonify({"code": 400, "msg": "缺少 record_ids 参数"}), 400

        results = {"reocr_success": 0, "reocr_failed": 0, "sync_success": 0, "sync_failed": 0, "errors": []}

        for rid in record_ids:
            # 1. 获取记录
            record = get_insurance_record(_db, rid)
            if not record:
                results["errors"].append({"id": rid, "error": "记录不存在"})
                results["reocr_failed"] += 1
                continue

            cos_url = record.get("cos_url", "")
            if not cos_url:
                results["errors"].append({"id": rid, "error": "无文件URL"})
                results["reocr_failed"] += 1
                continue

            # 2. 重新识别
            try:
                resp = http_requests.get(cos_url, timeout=60)
                if resp.status_code != 200:
                    results["errors"].append({"id": rid, "error": "文件下载失败"})
                    results["reocr_failed"] += 1
                    continue

                pdf_bytes = resp.content
                filename = record.get("filename", "file.pdf")
                ocr_result = _handler._do_ocr(pdf_bytes, filename)
                if not ocr_result.get("success"):
                    results["errors"].append({"id": rid, "error": f"OCR失败: {ocr_result.get('error', '')}"})
                    results["reocr_failed"] += 1
                    continue

                policy = ocr_result.get("policy", {})
                ocr_engine = ocr_result.get("ocr_engine", "pdfplumber")
                parsed_fields = policy.get("fields", {})
                doc_category = policy.get("doc_category", "")
                confidence = policy.get("confidence", 0.0)

                # 同车牌互补
                _handler._cross_fill_by_plate(parsed_fields, rid)

                # 字段映射
                company_short = parsed_fields.get("保险公司简称", "")
                mapped_fields = apply_mapping(parsed_fields, company_short)

                # 更新记录
                raw_text = policy.get("raw_text", "")
                update_insurance_record(_db, rid, {
                    "status": "done",
                    "ocr_engine": ocr_engine,
                    "doc_category": doc_category,
                    "confidence": confidence,
                    "parsed_fields": parsed_fields,
                    "mapped_fields": mapped_fields,
                    "raw_text": raw_text,
                    "error_message": "",
                })
                results["reocr_success"] += 1

            except Exception as e:
                results["errors"].append({"id": rid, "error": f"识别异常: {str(e)[:200]}"})
                results["reocr_failed"] += 1
                continue

            # 3. 同步钉钉（使用监控配置绑定关系，与自动同步一致）
            try:
                roomid = record.get("roomid", "")
                sender = record.get("sender", "")
                sender_name = record.get("sender_name", "")
                cos_url = record.get("cos_url", "")
                monitors = _handler._get_matched_monitors(roomid, sender)
                if not monitors:
                    logger.info("record_id=%d 无匹配监控配置，跳过钉钉同步", rid)
                    results["sync_failed"] += 1
                    results["errors"].append({"id": rid, "error": "无匹配的监控配置"})
                else:
                    synced = False
                    for monitor in monitors:
                        if not (monitor.get("dingtalk_base_id") and monitor.get("dingtalk_sheet_id")):
                            continue
                        # 使用监控配置自身的字段映射（如果有）
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
                        _handler._sync_to_dingtalk_v2(
                            rid, sync_fields,
                            sender_name=sender_name,
                            cos_url=cos_url,
                            doc_category=doc_category,
                            monitor=monitor,
                        )
                        synced = True
                    if synced:
                        update_insurance_record(_db, rid, {"dingtalk_synced": 1})
                        results["sync_success"] += 1
                    else:
                        results["sync_failed"] += 1
                        results["errors"].append({"id": rid, "error": "监控配置未绑定钉钉文档"})
            except Exception as e:
                results["errors"].append({"id": rid, "error": f"同步失败: {str(e)[:200]}"})
                results["sync_failed"] += 1

        msg_parts = []
        if results["reocr_success"]:
            msg_parts.append(f"识别成功 {results['reocr_success']} 条")
        if results["sync_success"]:
            msg_parts.append(f"同步成功 {results['sync_success']} 条")
        if results["reocr_failed"]:
            msg_parts.append(f"识别失败 {results['reocr_failed']} 条")
        if results["sync_failed"]:
            msg_parts.append(f"同步失败 {results['sync_failed']} 条")

        return jsonify({
            "code": 0,
            "data": results,
            "msg": "、".join(msg_parts) if msg_parts else "无操作",
        })
    except Exception as e:
        logger.exception("批量重新识别同步失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


# ============================================================
# 配置管理
# ============================================================

@insurance_bp.route("/api/insurance/config", methods=["GET"])
def get_config():
    """获取全部保单识别配置"""
    try:
        config = get_all_insurance_config(_db, max_value_len=10000)
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


@insurance_bp.route("/api/insurance/reload-ocr", methods=["POST"])
def reload_ocr():
    """重新加载所有 OCR 引擎配置（支持 config.yaml 热更新）"""
    try:
        if not _handler:
            return jsonify({"code": 500, "msg": "handler 未初始化"}), 500
        _handler.reload_all_ocr()
        # 返回各引擎状态
        engines = []
        if _handler._volc_ocr:
            engines.append("火山引擎: 已启用")
        if _handler._baidu_ocr:
            acc = {"accurate": "高精度版", "standard": "标准版"}.get(
                getattr(_handler._baidu_ocr, "accuracy", ""), "未知"
            )
            engines.append(f"百度OCR: {acc}")
        if not engines:
            engines.append("无可用引擎")
        return jsonify({"code": 0, "msg": "OCR 引擎已重载", "engines": engines})
    except Exception as e:
        logger.exception("重载 OCR 引擎失败")
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


# 配置状态缓存（60秒）
_config_status_cache = {"data": None, "time": 0}


@insurance_bp.route("/api/insurance/config/status", methods=["GET"])
def config_status():
    """检查配置状态（OCR、钉钉等关键配置是否就绪），60秒缓存"""
    import time as _time

    if _config_status_cache["data"] and (_time.time() - _config_status_cache["time"] < 60):
        return jsonify({"code": 0, "data": _config_status_cache["data"]})

    try:
        # 只查 config_key 是否存在 + value 长度，完全不读 LONGTEXT 内容
        conn = _db.pool.connection()
        try:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT config_key, LENGTH(config_value) as val_len "
                "FROM insurance_config "
                "WHERE config_key IN ("
                "  'baidu_ocr_app_id','baidu_ocr_api_key','baidu_ocr_secret_key','baidu_ocr_enabled',"
                "  'dingtalk_app_key','dingtalk_app_secret','dingtalk_operator_id'"
                ")"
            )
            existing = {row["config_key"]: row["val_len"] for row in cursor.fetchall()}
        finally:
            conn.close()

        baidu_ok = all(existing.get(k, 0) for k in ["baidu_ocr_app_id", "baidu_ocr_api_key", "baidu_ocr_secret_key"])
        dingtalk_ok = all(existing.get(k, 0) for k in ["dingtalk_app_key", "dingtalk_app_secret", "dingtalk_operator_id"])
        baidu_enabled = "baidu_ocr_enabled" not in existing or existing.get("baidu_ocr_enabled", 0) > 0

        # 钉钉目标数量：用监控配置表计数，不读大 JSON
        targets_count = 0
        try:
            from .monitor_config_db import list_monitor_configs
            monitors = list_monitor_configs(_db)
            targets_count = len([m for m in monitors if m.get("dingtalk_base_id")])
        except Exception:
            pass

        status = {
            "baidu_ocr": {
                "configured": baidu_ok,
                "enabled": baidu_enabled,
            },
            "dingtalk": {
                "configured": dingtalk_ok,
                "targets_count": targets_count,
            },
            "handler_ready": _handler is not None,
        }

        _config_status_cache["data"] = status
        _config_status_cache["time"] = _time.time()

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

        inserted = result.get("inserted_count", 0)
        updated = result.get("updated_count", 0)
        msg_parts = []
        if inserted:
            msg_parts.append(f"新增 {inserted} 条")
        if updated:
            msg_parts.append(f"更新 {updated} 条")
        msg = "、".join(msg_parts) if msg_parts else f"同步 {len(success_ids)} 条记录"

        return jsonify({
            "code": 0,
            "data": {
                "synced": len(success_ids),
                "failed": failed_ids,
                "detail": result,
            },
            "msg": msg,
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
        elif export_type == "nonpolicy":
            # 非保单类型：文件名 + 文档类型
            headers = ["文件名", "文档类型"]
            ws.append(headers)
            for inv in invoices:
                ws.append([
                    inv.get("file_name", ""),
                    inv.get("doc_category", "非保单"),
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


# ---- 批量下载任务管理 ----
import threading
import time as _time
import uuid
import zipfile

# 内存任务池（task_id → 任务状态），自动清理已完成超过 10 分钟的任务
_download_tasks = {}
_TASK_EXPIRE_SECONDS = 600


def _cleanup_expired_tasks():
    """清理过期任务（已完成超过 10 分钟）"""
    now = _time.time()
    expired = [tid for tid, t in _download_tasks.items()
               if t.get("finished_at") and now - t["finished_at"] > _TASK_EXPIRE_SECONDS]
    for tid in expired:
        _download_tasks.pop(tid, None)


def _do_batch_download(task_id, files_to_zip):
    """后台线程：逐个从 COS 下载文件并打包 ZIP"""
    import requests as http_requests

    task = _download_tasks[task_id]
    zip_buffer = io.BytesIO()
    used_names = {}

    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, item in enumerate(files_to_zip):
                task["current"] = i + 1
                task["current_file"] = item["filename"]
                try:
                    resp = http_requests.get(item["cos_url"], timeout=30)
                    if resp.status_code != 200:
                        task["failed"] += 1
                        continue
                    # 处理重名文件
                    name = item["filename"]
                    if name in used_names:
                        used_names[name] += 1
                        base, ext = (name.rsplit(".", 1) + [""])[:2]
                        name = f"{base}_{used_names[name]}.{ext}" if ext else f"{base}_{used_names[name]}"
                    else:
                        used_names[name] = 0
                    zf.writestr(name, resp.content)
                    task["success"] += 1
                except Exception:
                    task["failed"] += 1

        zip_buffer.seek(0)
        task["zip_data"] = zip_buffer
        task["status"] = "done"
    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)[:200]
    finally:
        task["finished_at"] = _time.time()


@insurance_bp.route("/api/insurance/batch-download/start", methods=["POST"])
def batch_download_start():
    """
    启动批量下载任务（异步）。
    请求体: {record_ids: [int, ...]}
    返回 task_id，前端轮询进度。
    """
    _cleanup_expired_tasks()

    try:
        body = request.get_json(force=True) or {}
        record_ids = body.get("record_ids", [])

        if not record_ids:
            return jsonify({"code": 400, "msg": "缺少 record_ids 参数"}), 400
        if len(record_ids) > 500:
            return jsonify({"code": 400, "msg": "单次下载不超过 500 条"}), 400

        # 收集文件信息
        files_to_zip = []
        for rid in record_ids:
            record = get_insurance_record(_db, rid)
            if not record:
                continue
            cos_url = record.get("cos_url", "")
            if not cos_url:
                continue
            filename = record.get("filename", f"record_{rid}.pdf")
            files_to_zip.append({"id": rid, "cos_url": cos_url, "filename": filename})

        if not files_to_zip:
            return jsonify({"code": 400, "msg": "没有可下载的文件"}), 400

        # 创建任务
        task_id = uuid.uuid4().hex[:12]
        _download_tasks[task_id] = {
            "status": "processing",
            "total": len(files_to_zip),
            "current": 0,
            "success": 0,
            "failed": 0,
            "current_file": "",
            "zip_data": None,
            "error": "",
            "finished_at": None,
        }

        # 启动后台线程
        t = threading.Thread(target=_do_batch_download, args=(task_id, files_to_zip), daemon=True)
        t.start()

        return jsonify({"code": 0, "data": {"task_id": task_id, "total": len(files_to_zip)}})
    except Exception as e:
        logger.exception("启动批量下载任务失败")
        return jsonify({"code": 500, "msg": str(e)}), 500


@insurance_bp.route("/api/insurance/batch-download/status/<task_id>", methods=["GET"])
def batch_download_status(task_id):
    """查询批量下载任务进度"""
    task = _download_tasks.get(task_id)
    if not task:
        return jsonify({"code": 404, "msg": "任务不存在或已过期"}), 404

    return jsonify({"code": 0, "data": {
        "status": task["status"],
        "total": task["total"],
        "current": task["current"],
        "success": task["success"],
        "failed": task["failed"],
        "current_file": task["current_file"],
        "error": task.get("error", ""),
    }})


@insurance_bp.route("/api/insurance/batch-download/file/<task_id>", methods=["GET"])
def batch_download_file(task_id):
    """下载已完成的 ZIP 文件"""
    task = _download_tasks.get(task_id)
    if not task:
        return jsonify({"code": 404, "msg": "任务不存在或已过期"}), 404
    if task["status"] != "done":
        return jsonify({"code": 400, "msg": "任务尚未完成"}), 400

    zip_data = task.get("zip_data")
    if not zip_data or zip_data.getbuffer().nbytes <= 22:
        return jsonify({"code": 400, "msg": "所有文件下载失败，无可下载内容"}), 400

    zip_data.seek(0)

    # 取出后清理任务数据（只允许下载一次，释放内存）
    _download_tasks.pop(task_id, None)

    return send_file(
        zip_data,
        mimetype="application/zip",
        as_attachment=True,
        download_name="保单文件批量下载.zip",
    )


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
