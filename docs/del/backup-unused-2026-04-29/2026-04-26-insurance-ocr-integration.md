# 保单识别集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 baoxianOcr 保单识别功能集成到 wxbot，实现指定群 PDF 自动识别并同步到钉钉多维表格

**Architecture:** 新增 `insurance/` 模块，通过 Queue 串行处理 PDF（控制内存），SDK 下载到内存后同时上传 COS 和做 OCR，识别结果自动同步钉钉。前端独立页面 `/insurance`，支持配置管理和手动上传。

**Tech Stack:** Flask Blueprint, SQLite, pdfplumber, 百度OCR API, 钉钉多维表 API, 腾讯云 COS

---

### Task 1: 创建 insurance 模块基础结构 + 数据库表

**Files:**
- Create: `insurance/__init__.py`
- Create: `insurance/db.py`
- Modify: `storage/db.py` (新增 insurance 相关表初始化方法)

- [ ] **Step 1: 创建 insurance/__init__.py**

```python
# insurance/__init__.py 留空
```

- [ ] **Step 2: 创建 insurance/db.py — 保单识别数据库操作**

```python
"""
保单识别数据库操作
insurance_records: 识别记录
insurance_config: 前端可配置项（监控群、钉钉目标等）
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def init_insurance_tables(db):
    """创建保单识别相关表（在 main.py 中调用）"""
    cursor = db.conn.cursor()

    # 识别记录表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS insurance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_seq INTEGER,
            roomid TEXT,
            room_name TEXT,
            sender TEXT,
            sender_name TEXT,
            filename TEXT,
            filesize INTEGER DEFAULT 0,
            cos_url TEXT,
            ocr_engine TEXT,
            doc_category TEXT,
            confidence REAL DEFAULT 0,
            parsed_fields TEXT,
            mapped_fields TEXT,
            dingtalk_synced INTEGER DEFAULT 0,
            dingtalk_target_id TEXT,
            dingtalk_record_id TEXT,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            source TEXT DEFAULT 'auto',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 配置表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS insurance_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_key TEXT UNIQUE,
            config_value TEXT,
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_insurance_records_roomid ON insurance_records(roomid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_insurance_records_status ON insurance_records(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_insurance_records_created ON insurance_records(created_at)")

    db.conn.commit()
    logger.info("保单识别数据库表初始化完成")


def save_insurance_record(db, record: dict) -> int:
    """保存识别记录，返回记录ID"""
    cursor = db.conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO insurance_records
        (msg_seq, roomid, room_name, sender, sender_name, filename, filesize,
         cos_url, ocr_engine, doc_category, confidence, parsed_fields, mapped_fields,
         dingtalk_synced, dingtalk_target_id, dingtalk_record_id,
         status, error_message, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.get("msg_seq"),
        record.get("roomid", ""),
        record.get("room_name", ""),
        record.get("sender", ""),
        record.get("sender_name", ""),
        record.get("filename", ""),
        record.get("filesize", 0),
        record.get("cos_url", ""),
        record.get("ocr_engine", ""),
        record.get("doc_category", ""),
        record.get("confidence", 0),
        json.dumps(record.get("parsed_fields", {}), ensure_ascii=False),
        json.dumps(record.get("mapped_fields", {}), ensure_ascii=False),
        record.get("dingtalk_synced", 0),
        record.get("dingtalk_target_id", ""),
        record.get("dingtalk_record_id", ""),
        record.get("status", "pending"),
        record.get("error_message", ""),
        record.get("source", "auto"),
        now, now,
    ))
    db.conn.commit()
    return cursor.lastrowid


def update_insurance_record(db, record_id: int, updates: dict):
    """更新识别记录"""
    updates["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 序列化 JSON 字段
    for key in ("parsed_fields", "mapped_fields"):
        if key in updates and isinstance(updates[key], dict):
            updates[key] = json.dumps(updates[key], ensure_ascii=False)

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [record_id]
    cursor = db.conn.cursor()
    cursor.execute(f"UPDATE insurance_records SET {set_clause} WHERE id = ?", values)
    db.conn.commit()


def query_insurance_records(db, page=1, page_size=50, roomid="", status="",
                            keyword="", source="") -> dict:
    """分页查询识别记录"""
    conditions = []
    params = []

    if roomid:
        conditions.append("roomid = ?")
        params.append(roomid)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if keyword:
        conditions.append("(filename LIKE ? OR mapped_fields LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if source:
        conditions.append("source = ?")
        params.append(source)

    where = " AND ".join(conditions)
    where_clause = f"WHERE {where}" if where else ""

    cursor = db.conn.cursor()
    cursor.execute(f"SELECT COUNT(*) as cnt FROM insurance_records {where_clause}", params)
    total = cursor.fetchone()["cnt"]
    pages = max(1, (total + page_size - 1) // page_size)

    offset = (page - 1) * page_size
    cursor.execute(
        f"SELECT * FROM insurance_records {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    )
    records = [dict(row) for row in cursor.fetchall()]

    return {"total": total, "pages": pages, "page": page, "records": records}


def get_insurance_record(db, record_id: int) -> dict:
    """获取单条记录"""
    cursor = db.conn.cursor()
    cursor.execute("SELECT * FROM insurance_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def get_insurance_config(db, key: str, default=None):
    """获取配置项"""
    cursor = db.conn.cursor()
    cursor.execute("SELECT config_value FROM insurance_config WHERE config_key = ?", (key,))
    row = cursor.fetchone()
    if row:
        try:
            return json.loads(row["config_value"])
        except (json.JSONDecodeError, TypeError):
            return row["config_value"]
    return default


def set_insurance_config(db, key: str, value):
    """设置配置项"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json_value = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    cursor = db.conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO insurance_config (config_key, config_value, updated_at)
        VALUES (?, ?, ?)
    """, (key, json_value, now))
    db.conn.commit()


def get_all_insurance_config(db) -> dict:
    """获取所有配置"""
    cursor = db.conn.cursor()
    cursor.execute("SELECT config_key, config_value FROM insurance_config")
    result = {}
    for row in cursor.fetchall():
        try:
            result[row["config_key"]] = json.loads(row["config_value"])
        except (json.JSONDecodeError, TypeError):
            result[row["config_key"]] = row["config_value"]
    return result


def get_insurance_stats(db) -> dict:
    """获取识别统计"""
    cursor = db.conn.cursor()

    cursor.execute("SELECT COUNT(*) as cnt FROM insurance_records")
    total = cursor.fetchone()["cnt"]

    cursor.execute("SELECT status, COUNT(*) as cnt FROM insurance_records GROUP BY status")
    status_stats = {row["status"]: row["cnt"] for row in cursor.fetchall()}

    cursor.execute("""
        SELECT roomid, room_name, COUNT(*) as cnt
        FROM insurance_records WHERE roomid != ''
        GROUP BY roomid ORDER BY cnt DESC
    """)
    room_stats = [dict(row) for row in cursor.fetchall()]

    return {"total": total, "status_stats": status_stats, "room_stats": room_stats}
```

- [ ] **Step 3: 验证模块导入**

Run: `cd /Users/take/Downloads/yun/web/wxbot && python -c "from insurance.db import init_insurance_tables; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add insurance/__init__.py insurance/db.py
git commit -m "feat: 新增保单识别模块数据库层（insurance_records + insurance_config 表）"
```

---

### Task 2: 迁移 OCR 核心文件（policy_parser, ocr_service, baidu_ocr, field_mapping）

**Files:**
- Copy: `baoxianOcr/policy_parser.py` → `insurance/policy_parser.py`
- Create: `insurance/ocr_service.py` (改造自 baoxianOcr/ocr_service.py，支持 bytes 输入)
- Create: `insurance/baidu_ocr.py` (改造自 baoxianOcr/baidu_ocr.py，支持 bytes 输入)
- Copy: `baoxianOcr/field_mapping.py` → `insurance/field_mapping.py`
- Copy: `baoxianOcr/field_mapping.json` → `insurance/config/field_mapping.json`

- [ ] **Step 1: 复制 policy_parser.py（不改动）**

```bash
cp /Users/take/Downloads/yun/web/baoxianOcr/policy_parser.py /Users/take/Downloads/yun/web/wxbot/insurance/policy_parser.py
```

- [ ] **Step 2: 创建 insurance/ocr_service.py（适配 bytes 输入）**

基于 baoxianOcr/ocr_service.py 改造，新增 `extract_text_from_pdf_bytes` 方法：

```python
"""
PDF文本提取服务
使用pdfplumber从PDF中提取文本层内容
支持 base64 和 bytes 两种输入方式
"""

import base64
import io
import re
from typing import Dict, Any

import pdfplumber


def clean_text(text: str) -> str:
    """清理文本中的多余空格（保留换行）"""
    for _ in range(3):
        text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
    text = re.sub(r'(\d)\s+([年月日时分秒])', r'\1\2', text)
    text = re.sub(r'([年月日])\s+(\d)', r'\1\2', text)
    return text


def _extract_from_file_obj(pdf_file, pdf_page: int = 1) -> Dict[str, Any]:
    """从文件对象中提取文本（内部方法）"""
    with pdfplumber.open(pdf_file) as pdf:
        if not pdf.pages:
            raise Exception("PDF文件没有任何页面")

        page_idx = min(pdf_page - 1, len(pdf.pages) - 1)
        page = pdf.pages[page_idx]
        raw_text = page.extract_text()

        if not raw_text:
            return {
                "success": False,
                "error": "无文本层（扫描件），暂不支持识别",
                "text": "",
                "page_count": len(pdf.pages),
            }

        text = clean_text(raw_text)

        return {
            "success": True,
            "text": text,
            "lines": text.split("\n"),
            "page_count": len(pdf.pages),
            "char_count": len(text),
        }


def extract_text_from_pdf(file_data_base64: str, pdf_page: int = 1) -> Dict[str, Any]:
    """从 base64 编码的 PDF 中提取文本"""
    pdf_bytes = base64.b64decode(file_data_base64)
    return _extract_from_file_obj(io.BytesIO(pdf_bytes), pdf_page)


def extract_text_from_pdf_bytes(pdf_bytes: bytes, pdf_page: int = 1) -> Dict[str, Any]:
    """从 bytes 中提取文本（用于自动识别流水线，避免 base64 编解码开销）"""
    return _extract_from_file_obj(io.BytesIO(pdf_bytes), pdf_page)
```

- [ ] **Step 3: 创建 insurance/baidu_ocr.py（适配 bytes 输入）**

基于 baoxianOcr/baidu_ocr.py 改造，新增 bytes 方法：

```python
"""
百度OCR服务 - 通用文字识别（高精度版）
当pdfplumber无法提取文本层时（扫描件），降级使用百度OCR识别
支持 base64 和 bytes 两种输入方式
"""

import base64
import time
import requests
from typing import Dict, Any, Optional


class BaiduOCR:
    """百度OCR客户端"""

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._access_token: Optional[str] = None
        self._token_expire_time: float = 0

    def _get_access_token(self) -> str:
        """获取access_token（带缓存，有效期30天）"""
        if self._access_token and time.time() < self._token_expire_time:
            return self._access_token

        resp = requests.post(self.TOKEN_URL, params={
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        })
        resp.raise_for_status()
        result = resp.json()

        if "access_token" not in result:
            raise Exception(f"百度OCR获取token失败: {result.get('error_description', '未知错误')}")

        self._access_token = result["access_token"]
        self._token_expire_time = time.time() + result.get("expires_in", 2592000) - 3600
        return self._access_token

    def _parse_result(self, result: dict) -> Dict[str, Any]:
        """解析OCR返回结果"""
        if "error_code" in result:
            return {
                "success": False,
                "error": f"百度OCR识别失败: {result.get('error_msg', '未知错误')} (错误码: {result['error_code']})",
                "text": "",
            }

        words_list = result.get("words_result", [])
        if not words_list:
            return {
                "success": False,
                "error": "百度OCR未识别到任何文字",
                "text": "",
            }

        text = "\n".join(item["words"] for item in words_list)
        return {
            "success": True,
            "text": text,
            "lines": [item["words"] for item in words_list],
            "words_count": result.get("words_result_num", 0),
            "char_count": len(text),
            "ocr_engine": "baidu",
        }

    def recognize_pdf(self, pdf_base64: str, page_num: int = 1) -> Dict[str, Any]:
        """识别PDF文件（base64输入）"""
        token = self._get_access_token()
        resp = requests.post(
            f"{self.OCR_URL}?access_token={token}",
            data={
                "pdf_file": pdf_base64,
                "pdf_file_num": str(page_num),
                "language_type": "CHN_ENG",
                "detect_direction": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())

    def recognize_pdf_bytes(self, pdf_bytes: bytes, page_num: int = 1) -> Dict[str, Any]:
        """识别PDF文件（bytes输入，自动转base64）"""
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
        return self.recognize_pdf(pdf_base64, page_num)

    def recognize_image(self, image_base64: str) -> Dict[str, Any]:
        """识别图片（base64输入）"""
        token = self._get_access_token()
        resp = requests.post(
            f"{self.OCR_URL}?access_token={token}",
            data={
                "image": image_base64,
                "language_type": "CHN_ENG",
                "detect_direction": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())
```

- [ ] **Step 4: 复制 field_mapping.py 并修改路径**

复制文件后，修改 `MAPPING_PATH` 指向 `insurance/config/field_mapping.json`：

```python
# 修改 field_mapping.py 的第9行
MAPPING_PATH = Path(__file__).parent / "config" / "field_mapping.json"
```

- [ ] **Step 5: 复制配置文件**

```bash
mkdir -p /Users/take/Downloads/yun/web/wxbot/insurance/config
cp /Users/take/Downloads/yun/web/baoxianOcr/field_mapping.json /Users/take/Downloads/yun/web/wxbot/insurance/config/field_mapping.json
```

- [ ] **Step 6: 验证导入**

Run: `cd /Users/take/Downloads/yun/web/wxbot && python -c "from insurance.ocr_service import extract_text_from_pdf_bytes; from insurance.baidu_ocr import BaiduOCR; from insurance.policy_parser import parse_policy_text; from insurance.field_mapping import load_mapping_config; print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add insurance/
git commit -m "feat: 迁移保单识别OCR核心模块（policy_parser, ocr_service, baidu_ocr, field_mapping）"
```

---

### Task 3: 迁移钉钉同步模块

**Files:**
- Create: `insurance/dingtalk_sync.py` (改造自 baoxianOcr/dingtalk_table.py，配置从数据库读取)
- Copy: `baoxianOcr/dingtalk_targets.json` → `insurance/config/dingtalk_targets.json`

- [ ] **Step 1: 创建 insurance/dingtalk_sync.py**

将 `dingtalk_table.py` 整体复制到 `insurance/dingtalk_sync.py`，**不修改核心逻辑**。`DingTalkTableService` 和 `parse_dingtalk_doc_url` 类保持不变，因为它们接受参数实例化，不依赖全局配置。

```bash
cp /Users/take/Downloads/yun/web/baoxianOcr/dingtalk_table.py /Users/take/Downloads/yun/web/wxbot/insurance/dingtalk_sync.py
```

- [ ] **Step 2: 复制钉钉目标配置**

```bash
cp /Users/take/Downloads/yun/web/baoxianOcr/dingtalk_targets.json /Users/take/Downloads/yun/web/wxbot/insurance/config/dingtalk_targets.json
```

- [ ] **Step 3: 验证导入**

Run: `cd /Users/take/Downloads/yun/web/wxbot && python -c "from insurance.dingtalk_sync import DingTalkTableService, parse_dingtalk_doc_url; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add insurance/dingtalk_sync.py insurance/config/dingtalk_targets.json
git commit -m "feat: 迁移钉钉多维表同步模块"
```

---

### Task 4: COS 存储扩展 — 支持 bytes 上传

**Files:**
- Modify: `core/cos_storage.py` (新增 `upload_bytes` 方法)

- [ ] **Step 1: 给 CosStorage 新增 upload_bytes 方法**

在 `core/cos_storage.py` 的 `upload_file` 方法后新增：

```python
def upload_bytes(self, data: bytes, cos_key_suffix: str) -> str:
    """
    直接上传字节流到COS（不落盘）
    Args:
        data: 文件字节流
        cos_key_suffix: COS 上的相对路径（如 insurance/roomid/sender/file.pdf）
    返回: 文件的访问URL
    """
    cos_key = f"{self.prefix}{cos_key_suffix}"
    try:
        self.client.put_object(
            Bucket=self.bucket,
            Key=cos_key,
            Body=data,
        )
        url = f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{cos_key}"
        logger.info("上传COS成功(bytes): %s", url)
        return url
    except Exception as e:
        logger.error("上传COS失败(bytes): %s", e)
        return ""
```

- [ ] **Step 2: 验证**

Run: `cd /Users/take/Downloads/yun/web/wxbot && python -c "from core.cos_storage import CosStorage; print(hasattr(CosStorage, 'upload_bytes')); print('OK')"`
Expected: `True` then `OK`

- [ ] **Step 3: Commit**

```bash
git add core/cos_storage.py
git commit -m "feat: COS存储新增upload_bytes方法，支持字节流直接上传（不落盘）"
```

---

### Task 5: SDK 扩展 — 支持下载到内存

**Files:**
- Modify: `sdk/finance_sdk.py` (新增 `get_media_data_bytes` 方法)

- [ ] **Step 1: 给 FinanceSDK 新增 get_media_data_bytes 方法**

在 `sdk/finance_sdk.py` 的 `get_media_data` 方法后新增：

```python
def get_media_data_bytes(self, sdk_file_id: str, proxy: str = "", passwd: str = "",
                         timeout: int = 10) -> tuple[int, bytes]:
    """
    下载媒体文件到内存（分块下载拼接为bytes）
    Args:
        sdk_file_id: 媒体文件的sdkfileid
        proxy: 代理地址
        passwd: 代理密码
        timeout: 超时秒数
    返回: (错误码, 文件bytes)
    """
    index_buf = b""
    chunks = []

    while True:
        media_data = self.dll.NewMediaData()
        try:
            ret = self.dll.GetMediaData(
                self.sdk,
                index_buf,
                sdk_file_id.encode("utf-8"),
                proxy.encode("utf-8"),
                passwd.encode("utf-8"),
                ctypes.c_int(timeout),
                media_data
            )
            if ret != 0:
                logger.error("下载媒体文件失败(内存), 错误码: %d", ret)
                return ret, b""

            # 读取数据块
            data_len = self.dll.GetDataLen(media_data)
            data_ptr = self.dll.GetData(media_data)
            chunks.append(ctypes.string_at(data_ptr, data_len))

            # 检查是否下载完成
            is_finish = self.dll.IsMediaDataFinish(media_data)
            if is_finish == 1:
                break

            # 获取下一块的索引
            index_buf = self.dll.GetOutIndexBuf(media_data)
        finally:
            self.dll.FreeMediaData(media_data)

    result = b"".join(chunks)
    logger.info("媒体文件下载到内存完成, 大小: %d bytes", len(result))
    return 0, result
```

- [ ] **Step 2: Commit**

```bash
git add sdk/finance_sdk.py
git commit -m "feat: SDK新增get_media_data_bytes方法，支持媒体文件下载到内存"
```

---

### Task 6: 保单识别处理器 — 队列串行消费

**Files:**
- Create: `insurance/handler.py`

- [ ] **Step 1: 创建 insurance/handler.py**

```python
"""
保单识别处理器
Queue 串行消费，控制内存峰值
流水线：SDK下载 → COS上传 → OCR → 解析 → 钉钉同步
"""

import base64
import gc
import logging
import os
import queue
import tempfile
import threading
from datetime import datetime

from insurance.db import (
    save_insurance_record, update_insurance_record,
    get_insurance_config,
)
from insurance.ocr_service import extract_text_from_pdf_bytes
from insurance.policy_parser import parse_policy_text
from insurance.field_mapping import apply_mapping
from insurance.dingtalk_sync import DingTalkTableService

logger = logging.getLogger(__name__)

# 单文件大小限制：超过此值走临时文件
MAX_MEMORY_SIZE = 20 * 1024 * 1024  # 20MB


class InsuranceHandler:
    """保单识别处理器（队列串行）"""

    def __init__(self, db, finance_sdk=None, sdk_config=None,
                 cos_storage=None, contacts=None):
        self.db = db
        self.finance_sdk = finance_sdk
        self.sdk_config = sdk_config or {}
        self.cos_storage = cos_storage
        self.contacts = contacts
        self._queue = queue.Queue(maxsize=100)
        self._worker = threading.Thread(target=self._consume, daemon=True)
        self._worker.start()
        self._baidu_ocr = None
        self._init_baidu_ocr()

    def _init_baidu_ocr(self):
        """初始化百度OCR客户端（从数据库配置读取）"""
        baidu_cfg = get_insurance_config(self.db, "baidu_ocr", {})
        if baidu_cfg.get("api_key") and baidu_cfg.get("secret_key"):
            from insurance.baidu_ocr import BaiduOCR
            self._baidu_ocr = BaiduOCR(
                api_key=baidu_cfg["api_key"],
                secret_key=baidu_cfg["secret_key"],
            )
            logger.info("保单识别-百度OCR客户端已初始化")

    def reload_baidu_ocr(self):
        """重载百度OCR配置（配置更新时调用）"""
        self._init_baidu_ocr()

    def get_watch_rooms(self) -> list:
        """获取监控群ID列表"""
        rooms = get_insurance_config(self.db, "watch_rooms", [])
        return [r["roomid"] for r in rooms if isinstance(r, dict)]

    def enqueue(self, msg: dict):
        """非阻塞投入队列"""
        try:
            self._queue.put_nowait(msg)
            logger.info("保单识别任务入队: seq=%s, file=%s", msg.get("seq"), msg.get("filename"))
        except queue.Full:
            logger.warning("保单识别队列已满，丢弃消息 seq=%s", msg.get("seq"))

    def _consume(self):
        """单线程串行消费"""
        while True:
            msg = self._queue.get()
            try:
                self._process(msg)
            except Exception as e:
                logger.error("保单识别处理异常: seq=%s, error=%s", msg.get("seq"), e, exc_info=True)
            finally:
                self._queue.task_done()

    def _process(self, msg: dict):
        """处理单个PDF消息"""
        seq = msg.get("seq")
        roomid = msg.get("roomid", "")
        sender = msg.get("sender", "")
        filename = msg.get("filename", "")
        sdkfileid = msg.get("sdkfileid", "")
        filesize = msg.get("filesize", 0)

        # 解析发送人和群名
        sender_name = ""
        room_name = ""
        if self.contacts:
            sender_name = self.contacts.get_name(sender)
            room_name = self.contacts.get_room_name(roomid)

        # 创建初始记录
        record_id = save_insurance_record(self.db, {
            "msg_seq": seq,
            "roomid": roomid,
            "room_name": room_name,
            "sender": sender,
            "sender_name": sender_name,
            "filename": filename,
            "filesize": filesize,
            "status": "processing",
            "source": "auto",
        })

        try:
            # 1. SDK 下载媒体文件
            pdf_bytes = self._download_media(sdkfileid, filesize)
            if not pdf_bytes:
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "error_message": "SDK下载媒体文件失败",
                })
                return

            # 2. 上传到 COS
            cos_url = self._upload_to_cos(pdf_bytes, roomid, sender, filename)
            if cos_url:
                update_insurance_record(self.db, record_id, {"cos_url": cos_url})

            # 3. OCR 识别
            ocr_result = self._do_ocr(pdf_bytes, filename)

            # 释放PDF内存
            del pdf_bytes
            gc.collect()

            if not ocr_result["success"]:
                update_insurance_record(self.db, record_id, {
                    "status": "failed",
                    "ocr_engine": ocr_result.get("ocr_engine", ""),
                    "error_message": ocr_result.get("error", "OCR识别失败"),
                })
                return

            policy = ocr_result["policy"]
            ocr_engine = ocr_result["ocr_engine"]

            # 4. 字段映射
            company_short = policy.get("fields", {}).get("保险公司简称", "")
            mapped = apply_mapping(policy.get("fields", {}), company_short)

            # 5. 更新记录
            update_insurance_record(self.db, record_id, {
                "status": "success",
                "ocr_engine": ocr_engine,
                "doc_category": policy.get("doc_category", ""),
                "confidence": policy.get("confidence", 0),
                "parsed_fields": policy.get("fields", {}),
                "mapped_fields": mapped,
            })

            # 6. 自动同步钉钉
            auto_sync = get_insurance_config(self.db, "auto_sync_enabled", False)
            if auto_sync:
                self._sync_to_dingtalk(record_id, mapped, policy.get("fields", {}))

            logger.info("保单识别完成: seq=%s, 保险公司=%s, 保单号=%s",
                        seq,
                        policy.get("fields", {}).get("保险公司", ""),
                        policy.get("fields", {}).get("保单号", ""))

        except Exception as e:
            logger.error("保单识别失败: record_id=%d, error=%s", record_id, e, exc_info=True)
            update_insurance_record(self.db, record_id, {
                "status": "failed",
                "error_message": str(e),
            })

    def _download_media(self, sdkfileid: str, filesize: int) -> bytes:
        """从SDK下载媒体文件到内存（大文件走临时文件）"""
        if not self.finance_sdk:
            logger.error("SDK未初始化，无法下载媒体文件")
            return b""

        proxy = self.sdk_config.get("proxy", "")
        passwd = self.sdk_config.get("proxy_passwd", "")
        timeout = self.sdk_config.get("timeout", 10)

        if filesize > MAX_MEMORY_SIZE:
            # 大文件：写临时文件再读回
            logger.info("大文件(%d bytes)走临时文件处理", filesize)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                ret, _ = self.finance_sdk.get_media_data(
                    sdk_file_id=sdkfileid, proxy=proxy,
                    passwd=passwd, timeout=timeout, save_path=tmp_path,
                )
                if ret != 0:
                    return b""
                with open(tmp_path, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        else:
            # 常规：直接下载到内存
            ret, data = self.finance_sdk.get_media_data_bytes(
                sdk_file_id=sdkfileid, proxy=proxy,
                passwd=passwd, timeout=timeout,
            )
            if ret != 0:
                return b""
            return data

    def _upload_to_cos(self, pdf_bytes: bytes, roomid: str, sender: str,
                       filename: str) -> str:
        """上传到COS，路径：insurance/{roomid}/{sender}/{filename}"""
        if not self.cos_storage:
            return ""

        # 构造COS路径
        cos_key = f"insurance/{roomid}/{sender}/{filename}"

        # 检查重名，追加时间戳
        if self.cos_storage.file_exists(cos_key):
            name, ext = os.path.splitext(filename)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            cos_key = f"insurance/{roomid}/{sender}/{name}_{timestamp}{ext}"

        return self.cos_storage.upload_bytes(pdf_bytes, cos_key)

    def _do_ocr(self, pdf_bytes: bytes, filename: str) -> dict:
        """执行OCR识别流水线（pdfplumber → 百度OCR降级）"""
        ocr_engine = "pdfplumber"

        # 1. pdfplumber 提取文本
        extract_result = extract_text_from_pdf_bytes(pdf_bytes)

        # 2. 失败则降级到百度OCR
        if not extract_result["success"] and self._baidu_ocr:
            logger.info("[%s] pdfplumber无文本层，降级百度OCR", filename)
            extract_result = self._baidu_ocr.recognize_pdf_bytes(pdf_bytes)
            ocr_engine = "baidu"

        if not extract_result["success"]:
            error = extract_result.get("error", "文本提取失败")
            if "无文本层" in error and not self._baidu_ocr:
                error += "（提示：配置百度OCR可自动识别扫描件）"
            return {"success": False, "error": error, "ocr_engine": ocr_engine}

        # 3. 解析保单字段
        policy = parse_policy_text(extract_result["text"])

        # 4. 质量检查：置信度为0或类型为"其他"时降级
        need_fallback = (
            not policy.get("fields")
            or policy.get("doc_category") == "其他"
            or policy.get("confidence", 0) == 0
        )
        if need_fallback and ocr_engine == "pdfplumber" and self._baidu_ocr:
            logger.info("[%s] pdfplumber效果不佳，降级百度OCR", filename)
            extract_result = self._baidu_ocr.recognize_pdf_bytes(pdf_bytes)
            ocr_engine = "baidu"
            if extract_result["success"]:
                policy = parse_policy_text(extract_result["text"])

        if not policy.get("fields") or policy.get("confidence", 0) == 0:
            return {"success": False, "error": "未识别到任何保单关键字段", "ocr_engine": ocr_engine}

        return {"success": True, "policy": policy, "ocr_engine": ocr_engine}

    def _sync_to_dingtalk(self, record_id: int, mapped_fields: dict,
                          parsed_fields: dict):
        """同步识别结果到钉钉"""
        try:
            dt_cfg = get_insurance_config(self.db, "dingtalk_app", {})
            targets = get_insurance_config(self.db, "dingtalk_targets", [])
            if not dt_cfg.get("app_key") or not targets:
                logger.debug("钉钉未配置，跳过同步")
                return

            # 同步到所有目标
            for target in targets:
                try:
                    service = DingTalkTableService(
                        app_key=dt_cfg["app_key"],
                        app_secret=dt_cfg["app_secret"],
                        base_id=target["base_id"],
                        sheet_id=target["sheet_id"],
                        operator_id=dt_cfg.get("operator_id", ""),
                    )

                    # 使用目标配置的字段列表，如果没有则使用mapped_fields的key
                    field_names = target.get("fields", list(mapped_fields.keys()))

                    # 构造发票格式
                    invoice = {"fields": {}}
                    for name in field_names:
                        # 优先从映射后字段取值，再从原始字段取值
                        value = mapped_fields.get(name, "") or parsed_fields.get(name, "")
                        if value:
                            invoice["fields"][name] = value

                    result = service.append_invoices(
                        field_names=field_names,
                        invoices=[invoice],
                    )

                    update_insurance_record(self.db, record_id, {
                        "dingtalk_synced": 1,
                        "dingtalk_target_id": target.get("id", ""),
                    })
                    logger.info("钉钉同步成功: target=%s, inserted=%d",
                                target.get("name"), result.get("inserted_count", 0))

                except Exception as e:
                    logger.error("钉钉同步失败: target=%s, error=%s", target.get("name"), e)

        except Exception as e:
            logger.error("钉钉同步异常: record_id=%d, error=%s", record_id, e)

    def process_manual_upload(self, pdf_bytes: bytes, filename: str,
                              file_type: str = "pdf", pdf_page: int = 1) -> dict:
        """
        手动上传识别（同步处理，立即返回结果）
        兼容原 baoxianOcr 的 /api/ocr 接口
        """
        ocr_engine = "pdfplumber"

        if file_type in ("jpg", "jpeg", "png", "bmp"):
            # 图片直接走百度OCR
            if not self._baidu_ocr:
                return {"success": False, "file_name": filename,
                        "error": "图片识别需要配置百度OCR", "invoices": []}
            pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            extract_result = self._baidu_ocr.recognize_image(pdf_b64)
            ocr_engine = "baidu"
        else:
            # PDF 走标准流水线
            ocr_result = self._do_ocr(pdf_bytes, filename)
            if not ocr_result["success"]:
                return {"success": False, "file_name": filename,
                        "error": ocr_result.get("error", ""), "invoices": []}

            # 上传 COS
            cos_url = ""
            if self.cos_storage:
                cos_key = f"insurance/manual/{filename}"
                cos_url = self.cos_storage.upload_bytes(pdf_bytes, cos_key)

            policy = ocr_result["policy"]

            # 保存记录
            save_insurance_record(self.db, {
                "filename": filename,
                "filesize": len(pdf_bytes),
                "cos_url": cos_url,
                "ocr_engine": ocr_result["ocr_engine"],
                "doc_category": policy.get("doc_category", ""),
                "confidence": policy.get("confidence", 0),
                "parsed_fields": policy.get("fields", {}),
                "status": "success",
                "source": "manual",
            })

            return {
                "success": True,
                "file_name": filename,
                "invoices": [policy],
                "char_count": 0,
                "ocr_engine": ocr_result["ocr_engine"],
            }

        # 图片路径的后续处理
        if not extract_result["success"]:
            return {"success": False, "file_name": filename,
                    "error": extract_result.get("error", ""), "invoices": []}

        policy = parse_policy_text(extract_result["text"])
        if not policy.get("fields") or policy.get("confidence", 0) == 0:
            return {"success": False, "file_name": filename,
                    "error": "未识别到任何保单关键字段", "invoices": []}

        save_insurance_record(self.db, {
            "filename": filename,
            "filesize": len(pdf_bytes),
            "ocr_engine": ocr_engine,
            "doc_category": policy.get("doc_category", ""),
            "confidence": policy.get("confidence", 0),
            "parsed_fields": policy.get("fields", {}),
            "status": "success",
            "source": "manual",
        })

        return {
            "success": True,
            "file_name": filename,
            "invoices": [policy],
            "char_count": extract_result.get("char_count", 0),
            "ocr_engine": ocr_engine,
        }
```

- [ ] **Step 2: Commit**

```bash
git add insurance/handler.py
git commit -m "feat: 保单识别处理器 - 队列串行消费、OCR流水线、钉钉自动同步"
```

---

### Task 7: Flask API 蓝图 — 保单识别 REST API

**Files:**
- Create: `insurance/api.py`

- [ ] **Step 1: 创建 insurance/api.py**

```python
"""
保单识别 API
Flask Blueprint，挂载在 /api/insurance/ 下
"""

import base64
import io
import json
import hashlib
import logging
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify, request, send_file

from insurance.db import (
    query_insurance_records, get_insurance_record,
    update_insurance_record, get_all_insurance_config,
    get_insurance_config, set_insurance_config,
    get_insurance_stats,
)
from insurance.field_mapping import (
    load_mapping_config, save_mapping_config, apply_mapping,
    get_available_ocr_fields, OUTPUT_COLUMNS,
)
from insurance.dingtalk_sync import DingTalkTableService, parse_dingtalk_doc_url
from insurance.policy_parser import get_extraction_rules

logger = logging.getLogger(__name__)

insurance_bp = Blueprint("insurance", __name__)

_db = None
_handler = None
_contacts = None


def init_insurance_api(db, handler=None, contacts=None):
    """初始化保单识别API"""
    global _db, _handler, _contacts
    _db = db
    _handler = handler
    _contacts = contacts


# ========== 识别记录 ==========

@insurance_bp.route("/api/insurance/records")
def list_records():
    """查询识别记录"""
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    roomid = request.args.get("roomid", "")
    status = request.args.get("status", "")
    keyword = request.args.get("keyword", "")
    source = request.args.get("source", "")

    result = query_insurance_records(
        _db, page=page, page_size=min(page_size, 200),
        roomid=roomid, status=status, keyword=keyword, source=source,
    )
    return jsonify(result)


@insurance_bp.route("/api/insurance/records/<int:record_id>")
def get_record(record_id):
    """获取单条记录详情"""
    record = get_insurance_record(_db, record_id)
    if not record:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify(record)


@insurance_bp.route("/api/insurance/stats")
def stats():
    """识别统计"""
    return jsonify(get_insurance_stats(_db))


# ========== 手动上传识别 ==========

@insurance_bp.route("/api/insurance/ocr", methods=["POST"])
def manual_ocr():
    """手动上传识别（兼容原 baoxianOcr 接口）"""
    if not _handler:
        return jsonify({"error": "保单识别模块未初始化"}), 503

    data = request.get_json()
    if not data or not data.get("file_data"):
        return jsonify({"error": "缺少file_data参数"}), 400

    file_data = data["file_data"]
    file_type = data.get("file_type", "pdf")
    file_name = data.get("file_name", "unknown.pdf")
    pdf_page = data.get("pdf_page", 1)

    if file_type not in ("pdf", "jpg", "jpeg", "png", "bmp"):
        return jsonify({"success": False, "file_name": file_name,
                        "error": "支持PDF和图片格式（jpg/png/bmp）", "invoices": []})

    # base64 解码
    pdf_bytes = base64.b64decode(file_data)

    result = _handler.process_manual_upload(
        pdf_bytes=pdf_bytes, filename=file_name,
        file_type=file_type, pdf_page=pdf_page,
    )
    return jsonify(result)


# ========== 重试 / 手动同步 ==========

@insurance_bp.route("/api/insurance/retry/<int:record_id>", methods=["POST"])
def retry_record(record_id):
    """重试失败的识别"""
    record = get_insurance_record(_db, record_id)
    if not record:
        return jsonify({"error": "记录不存在"}), 404
    if record["status"] not in ("failed",):
        return jsonify({"error": "只能重试失败的记录"}), 400
    if not record.get("msg_seq"):
        return jsonify({"error": "手动上传的记录不支持重试，请重新上传"}), 400

    # 从 messages 表获取原始消息信息
    cursor = _db.conn.cursor()
    cursor.execute("SELECT parsed_content FROM messages WHERE seq = ?", (record["msg_seq"],))
    row = cursor.fetchone()
    if not row:
        return jsonify({"error": "原始消息不存在"}), 404

    parsed = json.loads(row["parsed_content"] or "{}")

    # 重新入队
    _handler.enqueue({
        "seq": record["msg_seq"],
        "roomid": record["roomid"],
        "sender": record["sender"],
        "filename": record["filename"],
        "sdkfileid": parsed.get("sdkfileid", ""),
        "filesize": record.get("filesize", 0),
    })

    # 删除旧记录，让handler创建新记录
    cursor.execute("DELETE FROM insurance_records WHERE id = ?", (record_id,))
    _db.conn.commit()

    return jsonify({"success": True, "message": "已重新加入处理队列"})


@insurance_bp.route("/api/insurance/sync/<int:record_id>", methods=["POST"])
def sync_record(record_id):
    """手动触发钉钉同步"""
    record = get_insurance_record(_db, record_id)
    if not record:
        return jsonify({"error": "记录不存在"}), 404
    if record["status"] != "success":
        return jsonify({"error": "只能同步成功的记录"}), 400

    mapped_fields = json.loads(record.get("mapped_fields") or "{}")
    parsed_fields = json.loads(record.get("parsed_fields") or "{}")

    if not mapped_fields and not parsed_fields:
        return jsonify({"error": "没有可同步的字段数据"}), 400

    target_id = request.get_json(silent=True) or {}
    target_id = target_id.get("target_id", "")

    try:
        _handler._sync_to_dingtalk(record_id, mapped_fields, parsed_fields)
        return jsonify({"success": True, "message": "同步成功"})
    except Exception as e:
        return jsonify({"error": f"同步失败: {str(e)}"}), 500


# ========== 配置管理 ==========

@insurance_bp.route("/api/insurance/config")
def get_config():
    """获取所有配置"""
    config = get_all_insurance_config(_db)
    return jsonify(config)


@insurance_bp.route("/api/insurance/config", methods=["PUT"])
def update_config():
    """更新配置"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "缺少配置数据"}), 400

    for key, value in data.items():
        set_insurance_config(_db, key, value)

    # 如果更新了百度OCR配置，重新初始化客户端
    if "baidu_ocr" in data and _handler:
        _handler.reload_baidu_ocr()

    return jsonify({"success": True})


@insurance_bp.route("/api/insurance/rooms")
def list_rooms():
    """获取可选群列表（roomid + 群名）"""
    if not _contacts:
        return jsonify({"rooms": []})

    # 从消息表中获取所有出现过的群ID
    cursor = _db.conn.cursor()
    cursor.execute("""
        SELECT DISTINCT roomid FROM messages
        WHERE roomid IS NOT NULL AND roomid != ''
        ORDER BY roomid
    """)
    room_ids = [row["roomid"] for row in cursor.fetchall()]

    rooms = []
    for rid in room_ids:
        name = _contacts.get_room_name(rid)
        rooms.append({"roomid": rid, "room_name": name})

    return jsonify({"rooms": rooms})


# ========== 字段映射配置 ==========

@insurance_bp.route("/api/insurance/mapping/config")
def get_mapping():
    """获取字段映射配置"""
    config = load_mapping_config()
    return jsonify({
        "output_columns": config.get("output_columns", OUTPUT_COLUMNS),
        "default_mapping": config.get("default_mapping", {}),
        "company_mappings": config.get("company_mappings", {}),
        "available_ocr_fields": get_available_ocr_fields(),
    })


@insurance_bp.route("/api/insurance/mapping/save", methods=["POST"])
def save_mapping():
    """保存字段映射"""
    data = request.get_json()
    company = data.get("company", "")
    mapping = data.get("mapping", {})
    output_columns = data.get("output_columns", [])

    config = load_mapping_config()
    if not company:
        config["default_mapping"] = mapping
        if output_columns:
            config["output_columns"] = output_columns
    else:
        if "company_mappings" not in config:
            config["company_mappings"] = {}
        config["company_mappings"][company] = mapping

    save_mapping_config(config)
    return jsonify({"success": True, "company": company or "默认"})


@insurance_bp.route("/api/insurance/mapping/company/<company>", methods=["DELETE"])
def delete_company_mapping(company):
    """删除保司专属映射"""
    config = load_mapping_config()
    if company in config.get("company_mappings", {}):
        del config["company_mappings"][company]
        save_mapping_config(config)
    return jsonify({"success": True})


@insurance_bp.route("/api/insurance/mapping/preview", methods=["POST"])
def preview_mapping():
    """预览映射效果"""
    data = request.get_json()
    fields = data.get("fields", {})
    company_short = data.get("company_short", "")
    mapped = apply_mapping(fields, company_short)
    return jsonify({"mapped_fields": mapped})


# ========== 钉钉目标管理 ==========

@insurance_bp.route("/api/insurance/dingtalk/targets")
def list_targets():
    """钉钉同步目标列表"""
    targets = get_insurance_config(_db, "dingtalk_targets", [])
    return jsonify({"targets": targets})


@insurance_bp.route("/api/insurance/dingtalk/targets", methods=["POST"])
def add_target():
    """添加钉钉目标"""
    data = request.get_json()
    doc_url = data.get("doc_url", "")
    name = data.get("name", "")

    if not doc_url:
        return jsonify({"error": "请输入钉钉文档URL"}), 400

    base_id, sheet_id = parse_dingtalk_doc_url(doc_url)
    if not base_id:
        return jsonify({"error": "无法从URL中解析文档ID"}), 400
    if not sheet_id:
        return jsonify({"error": "无法从URL中解析数据表ID"}), 400

    targets = get_insurance_config(_db, "dingtalk_targets", [])
    for t in targets:
        if t["base_id"] == base_id and t["sheet_id"] == sheet_id:
            return jsonify({"error": f"该文档已存在: {t['name']}"}), 400

    target = {
        "id": hashlib.md5(f"{base_id}_{sheet_id}".encode()).hexdigest()[:8],
        "name": name.strip() or f"文档_{len(targets) + 1}",
        "doc_url": doc_url,
        "base_id": base_id,
        "sheet_id": sheet_id,
    }
    targets.append(target)
    set_insurance_config(_db, "dingtalk_targets", targets)
    return jsonify({"success": True, "target": target})


@insurance_bp.route("/api/insurance/dingtalk/targets/<target_id>", methods=["PUT"])
def update_target(target_id):
    """更新钉钉目标"""
    data = request.get_json()
    targets = get_insurance_config(_db, "dingtalk_targets", [])
    for t in targets:
        if t["id"] == target_id:
            if "name" in data:
                t["name"] = data["name"]
            if "fields" in data:
                t["fields"] = data["fields"]
            set_insurance_config(_db, "dingtalk_targets", targets)
            return jsonify({"success": True, "target": t})
    return jsonify({"error": "目标不存在"}), 404


@insurance_bp.route("/api/insurance/dingtalk/targets/<target_id>", methods=["DELETE"])
def delete_target(target_id):
    """删除钉钉目标"""
    targets = get_insurance_config(_db, "dingtalk_targets", [])
    targets = [t for t in targets if t["id"] != target_id]
    set_insurance_config(_db, "dingtalk_targets", targets)
    return jsonify({"success": True})


# ========== 同步 & 导出 ==========

@insurance_bp.route("/api/insurance/sync/check-duplicates", methods=["POST"])
def check_duplicates():
    """保单号去重检查"""
    data = request.get_json()
    invoices = data.get("invoices", [])
    target_id = data.get("target_id", "")

    try:
        dt_cfg = get_insurance_config(_db, "dingtalk_app", {})
        targets = get_insurance_config(_db, "dingtalk_targets", [])
        target = next((t for t in targets if t["id"] == target_id), targets[0] if targets else None)
        if not target or not dt_cfg.get("app_key"):
            return jsonify({"duplicates": []})

        service = DingTalkTableService(
            app_key=dt_cfg["app_key"],
            app_secret=dt_cfg["app_secret"],
            base_id=target["base_id"],
            sheet_id=target["sheet_id"],
            operator_id=dt_cfg.get("operator_id", ""),
        )
        duplicates = service.find_duplicates(invoices, key_field="保单号")
        return jsonify({"duplicates": duplicates})
    except Exception as e:
        logger.error("重复检查失败: %s", e)
        return jsonify({"duplicates": []})


@insurance_bp.route("/api/insurance/sync/dingtalk", methods=["POST"])
def sync_to_dingtalk():
    """手动批量同步到钉钉"""
    data = request.get_json()
    invoices = data.get("invoices", [])
    field_names = data.get("field_names", [])
    target_id = data.get("target_id", "")

    if not invoices:
        return jsonify({"error": "没有可同步的数据"}), 400

    try:
        dt_cfg = get_insurance_config(_db, "dingtalk_app", {})
        targets = get_insurance_config(_db, "dingtalk_targets", [])
        target = next((t for t in targets if t["id"] == target_id), targets[0] if targets else None)
        if not target or not dt_cfg.get("app_key"):
            return jsonify({"error": "未配置钉钉"}), 400

        service = DingTalkTableService(
            app_key=dt_cfg["app_key"],
            app_secret=dt_cfg["app_secret"],
            base_id=target["base_id"],
            sheet_id=target["sheet_id"],
            operator_id=dt_cfg.get("operator_id", ""),
        )
        result = service.append_invoices(field_names=field_names, invoices=invoices)
        return jsonify({
            "success": True,
            "synced_count": result["inserted_count"],
            "skipped_fields": result.get("skipped_fields", []),
            "detail": result,
        })
    except Exception as e:
        return jsonify({"error": f"同步失败: {str(e)}"}), 500


@insurance_bp.route("/api/insurance/export/excel", methods=["POST"])
def export_excel():
    """导出Excel"""
    import openpyxl

    data = request.get_json()
    invoices = data.get("invoices", [])
    field_names = data.get("field_names", [])
    export_type = data.get("export_type", "success")

    if not invoices:
        return jsonify({"error": "没有可导出的数据"}), 400

    wb = openpyxl.Workbook()
    ws = wb.active
    title_map = {"success": "识别成功", "abnormal": "提取异常", "failed": "识别失败"}
    ws.title = title_map.get(export_type, "识别结果")

    if export_type == "failed":
        ws.append(["文件名", "失败原因"])
        for inv in invoices:
            ws.append([inv.get("file_name", ""), inv.get("error", "")])
    else:
        headers = ["文件名"] + field_names
        ws.append(headers)
        for inv in invoices:
            fields = inv.get("fields", {})
            file_name = inv.get("file_name", "")
            row = [file_name] + [fields.get(name, "") for name in field_names]
            ws.append(row)

    # 自动列宽
    for col_idx, col_name in enumerate(ws[1], 1):
        max_len = len(str(col_name.value or ""))
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            for cell in row:
                cell_len = len(str(cell.value or ""))
                if cell_len > max_len:
                    max_len = cell_len
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = min(max_len + 4, 40)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename_map = {"success": "保单识别结果.xlsx", "abnormal": "提取异常清单.xlsx", "failed": "识别失败清单.xlsx"}
    filename = filename_map.get(export_type, "导出结果.xlsx")
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@insurance_bp.route("/api/insurance/extraction-rules")
def extraction_rules():
    """获取字段提取规则描述"""
    company_short = request.args.get("company_short", "")
    return jsonify(get_extraction_rules(company_short))


@insurance_bp.route("/api/insurance/config/status")
def config_status():
    """检查配置状态"""
    baidu_cfg = get_insurance_config(_db, "baidu_ocr", {})
    dt_cfg = get_insurance_config(_db, "dingtalk_app", {})
    targets = get_insurance_config(_db, "dingtalk_targets", [])
    watch_rooms = get_insurance_config(_db, "watch_rooms", [])

    return jsonify({
        "pdf_extract": True,
        "baidu_ocr": bool(baidu_cfg.get("api_key") and baidu_cfg.get("secret_key")),
        "dingtalk": bool(dt_cfg.get("app_key") and dt_cfg.get("app_secret") and targets),
        "dingtalk_base": bool(dt_cfg.get("app_key") and dt_cfg.get("app_secret")),
        "watch_rooms_count": len(watch_rooms),
        "targets_count": len(targets),
    })
```

- [ ] **Step 2: Commit**

```bash
git add insurance/api.py
git commit -m "feat: 保单识别REST API蓝图（记录查询、手动上传、配置管理、钉钉同步、Excel导出）"
```

---

### Task 8: 主入口集成 — 接入 fetcher + 注册蓝图

**Files:**
- Modify: `main.py` (初始化 insurance 模块，注册蓝图)
- Modify: `core/fetcher.py` (消息处理后检查是否触发保单识别)

- [ ] **Step 1: 修改 main.py — 初始化 insurance 模块**

在 `main.py` 的 import 部分新增：

```python
from insurance.db import init_insurance_tables
from insurance.handler import InsuranceHandler
from insurance.api import insurance_bp, init_insurance_api
```

在 `create_app` 函数中，`init_api(db, contacts=contacts, cos_storage=cos_storage)` 之后新增：

```python
    # 初始化保单识别模块
    init_insurance_tables(db)
    insurance_handler = InsuranceHandler(
        db=db, cos_storage=cos_storage, contacts=contacts,
    )

    # 注册保单识别API蓝图
    init_insurance_api(db, handler=insurance_handler, contacts=contacts)
    app.register_blueprint(insurance_bp)

    # 保单识别独立页面
    @app.route("/insurance")
    def insurance_page():
        return render_template("insurance.html")
```

在 SDK 初始化成功的分支中（`init_api(db, fetcher, ...)` 那行后面），把 SDK 和 config 传给 handler：

```python
        # 更新保单识别的SDK引用
        insurance_handler.finance_sdk = finance_sdk
        insurance_handler.sdk_config = sdk_config
```

在 `app.extensions["wxbot"]` 中新增：

```python
            "insurance_handler": insurance_handler,
```

同时把 fetcher 的引用传给它，以便 fetcher 调用。在 `fetcher = MessageFetcher(...)` 之后新增：

```python
        # 将 insurance_handler 绑定到 fetcher
        fetcher.insurance_handler = insurance_handler
```

- [ ] **Step 2: 修改 core/fetcher.py — 触发保单识别**

在 `_process_item` 方法末尾，`self.db.save_message(msg_seq, msg_data, parsed)` 之后新增检测逻辑：

```python
            # 检查是否需要触发保单识别
            self._check_insurance_trigger(msg_seq, msg_data, parsed)
```

在 `MessageFetcher` 类中新增方法：

```python
    def _check_insurance_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保单识别"""
        handler = getattr(self, "insurance_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        content = parsed.get("content", {})

        # 条件：文件类型 + PDF后缀 + 在监控群列表中
        if msgtype != "file":
            return

        filename = content.get("filename", "")
        if not filename.lower().endswith(".pdf"):
            return

        watch_rooms = handler.get_watch_rooms()
        if not watch_rooms or roomid not in watch_rooms:
            return

        logger.info("触发保单识别: seq=%d, room=%s, file=%s", seq, roomid, filename)
        handler.enqueue({
            "seq": seq,
            "roomid": roomid,
            "sender": parsed.get("from", ""),
            "filename": filename,
            "sdkfileid": content.get("sdkfileid", ""),
            "filesize": content.get("filesize", 0),
        })
```

- [ ] **Step 3: 验证启动**

Run: `cd /Users/take/Downloads/yun/web/wxbot && python -c "from main import load_config, create_app; print('import OK')"`
Expected: `import OK` (可能因 SDK 不可用有 warning，但不应报错)

- [ ] **Step 4: Commit**

```bash
git add main.py core/fetcher.py
git commit -m "feat: 主入口集成保单识别模块 - fetcher自动触发 + 蓝图注册 + 独立页面路由"
```

---

### Task 9: 更新依赖

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 更新 requirements.txt**

新增保单识别所需依赖：

```
flask>=3.0
pycryptodome>=3.20
PyYAML>=6.0
cos-python-sdk-v5>=1.9
pdfplumber>=0.11.4
openpyxl>=3.1.2
requests>=2.31.0
```

- [ ] **Step 2: Commit**

```bash
git add requirements.txt
git commit -m "feat: 新增保单识别依赖（pdfplumber, openpyxl, requests）"
```

---

### Task 10: 前端独立页面

**Files:**
- Create: `web/templates/insurance.html`

- [ ] **Step 1: 创建保单识别前端页面**

创建 `web/templates/insurance.html`，迁移自 baoxianOcr 的 `static/index.html`，适配以下变更：
- API 路径前缀从 `/api/` 改为 `/api/insurance/`
- 新增「自动识别记录」表格区域（调用 `/api/insurance/records`）
- 新增「设置」弹窗（监控群管理、钉钉目标、百度OCR配置、钉钉应用配置）
- 群名显示（通过 `/api/insurance/rooms` 获取群名列表）
- 新增重试、手动同步按钮
- 保留原有手动上传区域、字段映射配置、Excel导出、钉钉同步功能

此文件较大（1000+ 行 HTML/Vue/Tailwind），应基于 baoxianOcr 的 `static/index.html` 完整改造。

**核心改动点：**
1. 页面标题改为「保单识别系统」
2. 顶部 Tab 切换：「手动上传」|「自动识别记录」
3. 设置按钮打开配置弹窗
4. API 地址全部加 `/insurance` 前缀
5. 自动识别记录表格：群名、发送人、文件名、险种、保费、状态、操作

- [ ] **Step 2: Commit**

```bash
git add web/templates/insurance.html
git commit -m "feat: 保单识别独立前端页面（手动上传 + 自动记录 + 设置管理）"
```

---

### Task 11: 更新文档

**Files:**
- Modify: `CLAUDE.md` (新增 insurance 模块说明)
- Create: `docs/changelog/2026-04/2026-04-26-16-00更新记录.md`

- [ ] **Step 1: 更新 CLAUDE.md**

在架构部分新增 insurance 模块说明。

- [ ] **Step 2: 创建更新记录**

```markdown
# 2026-04-26 保单识别集成

## 需求说明
将 baoxianOcr 保单识别项目集成到 wxbot，实现：
- 指定企微群收到的 PDF 自动触发保单识别
- PDF 上传至 COS 存储桶（按群ID/发送人ID/文件名归类）
- 识别完成后自动同步到钉钉多维表格
- 保留手动上传识别功能，独立页面 /insurance

## 数据库变更
新增表：
- `insurance_records` — 保单识别记录
- `insurance_config` — 保单识别配置（前端可配）

## 代码变更
新增模块：
- `insurance/` — 保单识别模块（handler, api, db, ocr_service, baidu_ocr, policy_parser, field_mapping, dingtalk_sync）
- `web/templates/insurance.html` — 独立前端页面

修改文件：
- `main.py` — 初始化 insurance 模块，注册蓝图
- `core/fetcher.py` — 消息处理后检查是否触发保单识别
- `core/cos_storage.py` — 新增 upload_bytes 方法
- `sdk/finance_sdk.py` — 新增 get_media_data_bytes 方法
- `requirements.txt` — 新增 pdfplumber, openpyxl, requests

## 部署步骤
1. `pip install -r requirements.txt` 安装新依赖
2. 重启服务，数据库表自动创建
3. 访问 `/insurance` 页面，在设置中配置：
   - 监控群列表
   - 百度OCR（可选，用于扫描件）
   - 钉钉应用凭证
   - 钉钉同步目标文档
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/changelog/
git commit -m "docs: 保单识别集成文档和更新记录"
```
