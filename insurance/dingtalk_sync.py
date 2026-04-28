#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉多维表（AI表格）同步服务
封装钉钉多维表 notable API，支持插入记录和获取字段信息

API路径格式: /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/records
"""

import time
import re
import requests
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, List, Dict, Any, Tuple


def parse_dingtalk_doc_url(url: str) -> Tuple[str, str]:
    """
    从钉钉文档URL自动提取 base_id 和 sheet_id

    支持的URL格式：
    https://alidocs.dingtalk.com/i/nodes/{nodeId}?iframeQuery=viewId%3Dxxx%26sheetId%3Dyyy

    Returns:
        (base_id, sheet_id)
    """
    parsed = urlparse(url)

    # 提取 base_id（节点ID）：路径中 /nodes/ 后面的部分
    path_match = re.search(r'/nodes/([a-zA-Z0-9]+)', parsed.path)
    base_id = path_match.group(1) if path_match else ""

    # 提取 sheet_id：在 iframeQuery 参数中
    sheet_id = ""
    qs = parse_qs(parsed.query)
    iframe_query = qs.get("iframeQuery", [""])[0]
    if iframe_query:
        iframe_params = parse_qs(unquote(iframe_query))
        sheet_id = iframe_params.get("sheetId", [""])[0]

    return base_id, sheet_id


class DingTalkTableService:
    """钉钉多维表（AI表格）服务"""

    TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
    # 多维表 API 基础路径
    BASE_URL = "https://api.dingtalk.com/v1.0/notable/bases"

    def __init__(self, app_key: str, app_secret: str, base_id: str,
                 sheet_id: str, operator_id: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_id = base_id
        self.sheet_id = sheet_id
        self.operator_id = operator_id
        self._access_token: Optional[str] = None
        self._token_expire_time: float = 0

    def get_access_token(self, force_refresh: bool = False) -> str:
        """获取access_token（带缓存）"""
        current_time = time.time()
        if not force_refresh and self._access_token and current_time < self._token_expire_time:
            return self._access_token

        params = {"appkey": self.app_key, "appsecret": self.app_secret}
        response = requests.get(self.TOKEN_URL, params=params, timeout=10)
        response.raise_for_status()
        result = response.json()

        if result.get("errcode", 0) != 0:
            raise Exception(f"获取钉钉token失败: {result.get('errmsg', '未知错误')}")

        self._access_token = result["access_token"]
        self._token_expire_time = current_time + 7200 - 300
        return self._access_token

    def _headers(self) -> Dict[str, str]:
        """构建请求头"""
        return {
            "x-acs-dingtalk-access-token": self.get_access_token(),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """统一请求方法，带详细错误日志"""
        response = requests.request(method, url, headers=self._headers(),
                                    timeout=kwargs.pop("timeout", 15), **kwargs)
        if not response.ok:
            # 打印完整错误信息
            try:
                error_body = response.json()
            except Exception:
                error_body = response.text
            error_msg = (f"钉钉API请求失败 [{response.status_code}]\n"
                         f"  URL: {url}\n"
                         f"  响应: {error_body}")
            print(error_msg)
            raise Exception(error_msg)
        return response.json()

    def get_all_sheets(self) -> List[Dict[str, Any]]:
        """
        获取多维表中所有数据表
        GET /v1.0/notable/bases/{baseId}/sheets?operatorId=xxx
        """
        url = f"{self.BASE_URL}/{self.base_id}/sheets"
        params = {"operatorId": self.operator_id}
        result = self._request("GET", url, params=params)
        return result.get("value", [])

    def get_all_fields(self, sheet_id: str = None) -> List[Dict[str, Any]]:
        """
        获取指定数据表的所有字段
        GET /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/fields?operatorId=xxx
        """
        sid = sheet_id or self.sheet_id
        url = f"{self.BASE_URL}/{self.base_id}/sheets/{sid}/fields"
        params = {"operatorId": self.operator_id}
        result = self._request("GET", url, params=params)
        return result.get("value", [])

    def create_field(self, name: str, field_type: str = "Text",
                     sheet_id: str = None) -> Dict[str, Any]:
        """
        创建字段
        POST /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/fields?operatorId=xxx

        Args:
            name: 字段名称
            field_type: 字段类型，默认 "Text"（文本类型）
            sheet_id: 数据表ID

        Returns:
            API响应
        """
        sid = sheet_id or self.sheet_id
        url = f"{self.BASE_URL}/{self.base_id}/sheets/{sid}/fields"
        params = {"operatorId": self.operator_id}
        body = {"name": name, "type": field_type}
        return self._request("POST", url, params=params, json=body)

    def ensure_fields_exist(self, field_names: List[str],
                            sheet_id: str = None) -> Dict[str, Any]:
        """
        确保多维表中存在指定的字段，不存在的尝试自动创建

        Args:
            field_names: 需要的字段名列表

        Returns:
            {"existing": [...], "created": [...], "missing": [...]}
        """
        existing_fields = self.get_all_fields(sheet_id)
        existing_names = {f.get("name", "") for f in existing_fields}

        missing = [name for name in field_names if name not in existing_names]
        if not missing:
            return {"existing": list(existing_names), "created": [], "missing": []}

        # 尝试创建第一个缺失字段，测试API是否支持
        created = []
        for name in missing:
            try:
                self.create_field(name=name, field_type="text", sheet_id=sheet_id)
                created.append(name)
            except Exception as e:
                error_str = str(e)
                if "not supported" in error_str.lower():
                    # API不支持自动创建字段，停止尝试，返回缺失列表
                    print(f"钉钉API暂不支持自动创建字段，需手动创建缺失字段: {missing}")
                    break
                print(f"创建字段 '{name}' 失败: {e}")
                break  # 第一个失败就停止

        still_missing = [n for n in missing if n not in created]
        return {"existing": list(existing_names), "created": created, "missing": still_missing}

    def list_records(self, sheet_id: str = None) -> List[Dict[str, Any]]:
        """
        获取多维表中的所有记录（自动分页，每页最多100条）
        GET /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/records?operatorId=xxx
        """
        sid = sheet_id or self.sheet_id
        url = f"{self.BASE_URL}/{self.base_id}/sheets/{sid}/records"
        all_records = []
        next_token = None

        while True:
            params = {"operatorId": self.operator_id, "maxResults": 100}
            if next_token:
                params["nextToken"] = next_token

            result = self._request("GET", url, params=params)

            # 兼容多种返回格式
            records = result.get("records", result.get("value", []))
            if records:
                if not all_records:
                    # 首页打印结构用于调试
                    print(f"[查重] 第一条记录结构: {list(records[0].keys())}")
                    fields = records[0].get("fields", records[0])
                    print(f"[查重] 第一条记录字段: {fields}")
                all_records.extend(records)

            # 检查是否有下一页
            next_token = result.get("nextToken")
            if not next_token:
                break

        print(f"[查重] 共获取 {len(all_records)} 条已有记录")
        return all_records

    def find_duplicates(self, invoices: List[Dict[str, Any]],
                        key_field: str = "发票号码") -> List[str]:
        """
        检查哪些发票已存在于多维表中

        Args:
            invoices: 待插入的发票数据
            key_field: 用于去重的字段名

        Returns:
            已存在的发票号码列表
        """
        existing_map = self._get_existing_key_map(key_field)
        duplicates = []
        for inv in invoices:
            fields = inv.get("fields", {})
            key_val = fields.get(key_field, "")
            if key_val and str(key_val).strip() in existing_map:
                duplicates.append(str(key_val).strip())

        print(f"[查重] 发现重复: {duplicates}")
        return duplicates

    def _get_existing_key_map(self, key_field: str) -> Dict[str, str]:
        """
        获取多维表中已有记录的 key_field → recordId 映射。
        返回: {"保单号值": "recordId", ...}
        """
        try:
            existing_records = self.list_records()
        except Exception as e:
            print(f"[查重] 获取已有记录失败: {e}")
            return {}

        key_map = {}
        for record in existing_records:
            fields = record.get("fields", record)
            record_id = record.get("id", record.get("recordId", ""))
            key_val = fields.get(key_field, "")
            if key_val and record_id:
                key_map[str(key_val).strip()] = record_id

        print(f"[查重] 已有记录({len(key_map)}个)")
        return key_map

    def insert_records(self, records: List[Dict[str, Any]],
                       sheet_id: str = None) -> Dict[str, Any]:
        """
        向多维表插入记录
        POST /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/records?operatorId=xxx

        Args:
            records: 记录列表，每条记录格式: {"fields": {"字段名": "值", ...}}
            sheet_id: 数据表ID，默认使用初始化时的sheet_id

        Returns:
            API响应
        """
        sid = sheet_id or self.sheet_id
        url = f"{self.BASE_URL}/{self.base_id}/sheets/{sid}/records"
        params = {"operatorId": self.operator_id}
        body = {"records": records}
        return self._request("POST", url, params=params, json=body, timeout=30)

    def update_records(self, records: List[Dict[str, Any]],
                       sheet_id: str = None) -> Dict[str, Any]:
        """
        批量更新多维表记录
        PUT /v1.0/notable/bases/{baseId}/sheets/{sheetIdOrName}/records?operatorId=xxx
        请求体: {"records": [{"id": "recordId", "fields": {...}}, ...]}
        """
        sid = sheet_id or self.sheet_id
        url = f"{self.BASE_URL}/{self.base_id}/sheets/{sid}/records"
        params = {"operatorId": self.operator_id}
        body = {"records": records}
        return self._request("PUT", url, params=params, json=body, timeout=30)

    def append_invoices(self, field_names: List[str],
                        invoices: List[Dict[str, Any]],
                        key_field: str = "保单号") -> Dict[str, Any]:
        """
        将发票识别结果追加到多维表（自动创建缺失字段）。
        相同保单号已存在时自动更新而非重复插入。

        Args:
            field_names: 字段名列表
            invoices: 发票数据列表，每项包含 {"fields": {"字段名": "值"}}
            key_field: 去重字段名，默认"保单号"

        Returns:
            API响应，包含插入/更新的记录数和字段创建信息
        """
        # 检查多维表现有字段，尝试创建缺失的
        field_result = self.ensure_fields_exist(field_names)
        if field_result["created"]:
            print(f"自动创建字段: {field_result['created']}")

        # 确定可用的字段（已存在 + 新创建的）
        available_fields = set(field_result["existing"]) | set(field_result["created"])
        # 只使用多维表中已存在的字段
        usable_fields = [name for name in field_names if name in available_fields]
        skipped_fields = [name for name in field_names if name not in available_fields]
        if skipped_fields:
            print(f"以下字段在多维表中不存在，已跳过: {skipped_fields}")

        if not usable_fields:
            raise Exception(
                f"多维表中没有匹配的字段，请先在钉钉多维表中手动创建以下字段：\n"
                f"{', '.join(field_names[:10])}..."
            )

        # 获取已有记录的保单号 → recordId 映射，用于去重更新
        existing_map = self._get_existing_key_map(key_field)

        # 构建多维表记录格式，区分新增和更新
        to_insert = []
        to_update = []  # (record_id, fields)
        for inv in invoices:
            fields = inv.get("fields", {})
            record_fields = {}
            for name in usable_fields:
                value = fields.get(name, "")
                if value:
                    record_fields[name] = value
            if not record_fields:
                continue

            # 检查是否已存在
            key_val = fields.get(key_field, "")
            existing_record_id = existing_map.get(str(key_val).strip()) if key_val else None
            if existing_record_id:
                to_update.append((existing_record_id, record_fields))
            else:
                to_insert.append({"fields": record_fields})

        batch_size = 100

        # 批量更新已有记录（每批最多100条）
        total_updated = 0
        for i in range(0, len(to_update), batch_size):
            batch = [{"id": rid, "fields": flds} for rid, flds in to_update[i:i + batch_size]]
            try:
                self.update_records(batch)
                total_updated += len(batch)
            except Exception as e:
                print(f"[同步] 批量更新失败: {e}")

        # 分批插入新记录
        total_inserted = 0
        last_result = {}

        for i in range(0, len(to_insert), batch_size):
            batch = to_insert[i:i + batch_size]
            last_result = self.insert_records(batch)
            total_inserted += len(batch)

        if total_updated:
            print(f"[同步] 更新 {total_updated} 条，新增 {total_inserted} 条")

        return {
            "success": True,
            "inserted_count": total_inserted,
            "updated_count": total_updated,
            "created_fields": field_result["created"],
            "skipped_fields": skipped_fields,
            "detail": last_result,
        }

    def is_configured(self) -> bool:
        """检查是否已配置钉钉凭证"""
        return bool(self.app_key and self.app_secret and self.base_id
                    and self.sheet_id and self.operator_id)
