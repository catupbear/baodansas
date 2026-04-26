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
        try:
            existing_records = self.list_records()
        except Exception as e:
            print(f"[查重] 获取已有记录失败: {e}")
            return []

        # 收集已有的发票号码（兼容多种数据结构）
        existing_keys = set()
        for record in existing_records:
            # 记录可能直接是 fields，也可能嵌套在 fields 字段中
            fields = record.get("fields", record)
            key_val = fields.get(key_field, "")
            if key_val:
                existing_keys.add(str(key_val).strip())

        print(f"[查重] 已有发票号码({len(existing_keys)}个): {existing_keys}")

        # 找出重复的
        duplicates = []
        for inv in invoices:
            fields = inv.get("fields", {})
            key_val = fields.get(key_field, "")
            if key_val and str(key_val).strip() in existing_keys:
                duplicates.append(str(key_val).strip())

        print(f"[查重] 发现重复: {duplicates}")
        return duplicates

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

    def append_invoices(self, field_names: List[str],
                        invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        将发票识别结果追加到多维表（自动创建缺失字段）

        Args:
            field_names: 字段名列表
            invoices: 发票数据列表，每项包含 {"fields": {"字段名": "值"}}

        Returns:
            API响应，包含插入的记录数和字段创建信息
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

        # 构建多维表记录格式（只写入可用字段）
        records = []
        for inv in invoices:
            fields = inv.get("fields", {})
            record_fields = {}
            for name in usable_fields:
                value = fields.get(name, "")
                if value:  # 只写入有值的字段
                    record_fields[name] = value
            if record_fields:  # 跳过空记录
                records.append({"fields": record_fields})

        # 分批插入（每批最多100条，避免超限）
        batch_size = 100
        total_inserted = 0
        last_result = {}

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            last_result = self.insert_records(batch)
            total_inserted += len(batch)

        return {
            "success": True,
            "inserted_count": total_inserted,
            "created_fields": field_result["created"],
            "skipped_fields": skipped_fields,
            "detail": last_result,
        }

    def is_configured(self) -> bool:
        """检查是否已配置钉钉凭证"""
        return bool(self.app_key and self.app_secret and self.base_id
                    and self.sheet_id and self.operator_id)
