# 保险报价模块（quote/）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 监控企业微信群中的图片和文本消息，自动识别行驶证/身份证，配对后提交保险预报价接口。

**Architecture:** 独立的 `quote/` 模块，通过 `fetcher.py` 新增触发点接入消息流水线。使用内存字典按 `(group_id, sender)` 维度管理多套证件数据，支持同一人1分钟内发多套不同车主的证件。通过外部 API 获取监控绑定列表（1分钟缓存），百度 OCR 识别行驶证和身份证。

**Tech Stack:** Python 3, Flask, requests, 百度 OCR API（行驶证+身份证）, 腾讯云 COS, threading

---

## 外部接口

### GET /insurance/wechat-bindings/all

无需 token，返回全部绑定配置。

```json
{
  "ok": 0,
  "data": [
    {
      "id": 1,
      "company_id": 1,
      "wechat_id": "wxid_abc123",
      "wechat_name": "张三",
      "group_name": "车队群A",
      "group_id": "gh_xxx123",
      "platform_user_id": 3,
      "auto_quote": 1,
      "template_id": 5,
      "pre_template_id": 2,
      "newtime": "2024-01-01 10:00:00",
      "uptime": "2024-01-01 10:00:00"
    }
  ]
}
```

### POST /insurance/pre-quote

无需 token，批量提交预报价订单。

**必填字段（⭐）：** `vehicle_info.plate_number`, `vehicle_info.vin`, `vehicle_info.engine_number`, `vehicle_info.first_register_date`, `vehicle_info.model`, `owner_info.name`, `owner_info.id_number`, `owner_info.id_card_image_cos_url`

缺任意⭐字段 → state=-3（参数不全），全齐 → state=-2（预报价）

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `quote/__init__.py` | 模块入口 |
| `quote/binding.py` | 调用 wechat-bindings/all 接口 + 1分钟缓存 |
| `quote/ocr.py` | 百度行驶证/身份证 OCR 封装 |
| `quote/matcher.py` | 多套证件配对逻辑（按姓名拆分套、身份证反面就近归组、超时检查线程） |
| `quote/handler.py` | 队列消费主逻辑：图片下载→COS上传→OCR→配对→提交 |
| `core/fetcher.py` | 修改：新增 `_check_quote_trigger` 触发点 |
| `main.py` | 修改：初始化 quote 模块并绑定到 fetcher |

不修改现有 `insurance/` 模块。不新建数据库表。

---

### Task 1: 创建 quote/binding.py — 监控绑定列表

**Files:**
- Create: `quote/__init__.py`
- Create: `quote/binding.py`

- [ ] **Step 1: 创建模块入口**

```python
# quote/__init__.py
```

- [ ] **Step 2: 实现 binding.py**

```python
# quote/binding.py
"""
微信群-保险账号绑定关系查询
调用外部接口获取监控列表，1分钟缓存
"""

import time
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://riskapi.wuhuxiche.com"


class BindingService:
    """微信绑定关系服务"""

    def __init__(self):
        self._cache: List[Dict] = []
        self._cache_time: float = 0
        self._cache_ttl: int = 60  # 缓存1分钟

    def _fetch_bindings(self) -> List[Dict]:
        """从远程接口获取绑定列表"""
        try:
            resp = requests.get(
                f"{BASE_URL}/insurance/wechat-bindings/all",
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") != 0:
                logger.error("获取绑定列表失败: %s", result.get("msg", "未知错误"))
                return []
            return result.get("data", [])
        except Exception as e:
            logger.error("请求绑定列表异常: %s", e)
            return []

    def get_bindings(self) -> List[Dict]:
        """获取绑定列表（带缓存）"""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache
        self._cache = self._fetch_bindings()
        self._cache_time = now
        logger.info("刷新绑定列表缓存，共 %d 条", len(self._cache))
        return self._cache

    def find_binding(self, group_id: str, sender: str) -> Optional[Dict]:
        """
        查找匹配的绑定记录
        Args:
            group_id: 微信群ID（roomid）
            sender: 发送人微信ID
        Returns:
            匹配的绑定记录，未匹配返回 None
        """
        bindings = self.get_bindings()
        for b in bindings:
            if b.get("group_id") == group_id and b.get("wechat_id") == sender:
                return b
        return None
```

- [ ] **Step 3: Commit**

```bash
git add quote/__init__.py quote/binding.py
git commit -m "feat(quote): 添加微信绑定关系查询服务，支持1分钟缓存"
```

---

### Task 2: 创建 quote/ocr.py — 行驶证/身份证 OCR

**Files:**
- Create: `quote/ocr.py`

- [ ] **Step 1: 实现 ocr.py**

```python
# quote/ocr.py
"""
百度OCR - 行驶证识别 & 身份证识别
行驶证API: https://cloud.baidu.com/doc/OCR/s/yk3h7y3ks
身份证API: https://cloud.baidu.com/doc/OCR/s/rk3h7xzck
"""

import base64
import time
import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class QuoteOCR:
    """保险报价证件OCR识别"""

    TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    VEHICLE_LICENSE_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vehicle_license"
    IDCARD_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/idcard"

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self._access_token: Optional[str] = None
        self._token_expire_time: float = 0

    def _get_access_token(self) -> str:
        """获取 access_token（带缓存，有效期30天）"""
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

    def recognize_vehicle_license(self, image_bytes: bytes) -> Dict[str, Any]:
        """
        识别行驶证
        Returns:
            成功: {"success": True, "type": "vehicle_license", "data": {字段...}}
            失败: {"success": False, "type": "vehicle_license", "error": "原因"}
        """
        token = self._get_access_token()
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        try:
            resp = requests.post(
                f"{self.VEHICLE_LICENSE_URL}?access_token={token}",
                data={
                    "image": image_base64,
                    "detect_direction": "true",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            if "error_code" in result:
                return {
                    "success": False,
                    "type": "vehicle_license",
                    "error": f"错误码{result['error_code']}: {result.get('error_msg', '')}",
                }

            words = result.get("words_result", {})
            if not words:
                return {"success": False, "type": "vehicle_license", "error": "未识别到行驶证字段"}

            plate = words.get("号牌号码", {}).get("words", "")
            vin = words.get("车辆识别代号", {}).get("words", "")
            owner = words.get("所有人", {}).get("words", "")

            # 至少要有车牌号或车架号才认为识别成功
            if not plate and not vin:
                return {"success": False, "type": "vehicle_license", "error": "缺少车牌号和车架号，非有效行驶证"}

            return {
                "success": True,
                "type": "vehicle_license",
                "data": {
                    "plate_number": plate,
                    "vin": vin,
                    "engine_number": words.get("发动机号码", {}).get("words", ""),
                    "first_register_date": words.get("注册日期", {}).get("words", ""),
                    "model": words.get("品牌型号", {}).get("words", ""),
                    "owner_name": owner,  # 用于姓名配对，不提交到 pre-quote
                },
            }
        except requests.RequestException as e:
            return {"success": False, "type": "vehicle_license", "error": str(e)}

    def recognize_idcard(self, image_bytes: bytes) -> Dict[str, Any]:
        """
        识别身份证（先尝试正面，失败再尝试反面）
        Returns:
            成功: {"success": True, "type": "idcard", "side": "front/back", "data": {字段...}}
            失败: {"success": False, "type": "idcard", "error": "原因"}
        """
        token = self._get_access_token()
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        front_result = self._call_idcard(token, image_base64, "front")
        if front_result.get("success"):
            return front_result

        back_result = self._call_idcard(token, image_base64, "back")
        if back_result.get("success"):
            return back_result

        return {"success": False, "type": "idcard", "error": "正反面均未识别到有效身份证信息"}

    def _call_idcard(self, token: str, image_base64: str, side: str) -> Dict[str, Any]:
        """调用身份证识别API"""
        try:
            resp = requests.post(
                f"{self.IDCARD_URL}?access_token={token}",
                data={
                    "image": image_base64,
                    "id_card_side": side,
                    "detect_direction": "true",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            if "error_code" in result:
                return {
                    "success": False,
                    "type": "idcard",
                    "error": f"错误码{result['error_code']}: {result.get('error_msg', '')}",
                }

            words = result.get("words_result", {})
            if not words:
                return {"success": False, "type": "idcard", "error": f"身份证{side}未识别到字段"}

            if side == "front":
                name = words.get("姓名", {}).get("words", "")
                id_number = words.get("公民身份号码", {}).get("words", "")
                if not name and not id_number:
                    return {"success": False, "type": "idcard", "error": "缺少姓名和身份证号，非有效身份证正面"}
                return {
                    "success": True,
                    "type": "idcard",
                    "side": "front",
                    "data": {
                        "name": name,
                        "id_number": id_number,
                    },
                }
            else:  # back
                issue_date = words.get("签发日期", {}).get("words", "")
                expiry_date = words.get("失效日期", {}).get("words", "")
                if not issue_date and not expiry_date:
                    return {"success": False, "type": "idcard", "error": "缺少签发/失效日期，非有效身份证反面"}
                return {
                    "success": True,
                    "type": "idcard",
                    "side": "back",
                    "data": {
                        "id_card_start_date": issue_date,
                        "id_card_end_date": expiry_date,
                    },
                }
        except requests.RequestException as e:
            return {"success": False, "type": "idcard", "error": str(e)}
```

- [ ] **Step 2: Commit**

```bash
git add quote/ocr.py
git commit -m "feat(quote): 添加百度OCR行驶证和身份证识别封装"
```

---

### Task 3: 创建 quote/matcher.py — 多套证件配对与超时提交

**Files:**
- Create: `quote/matcher.py`

**设计要点：**
- `_pending` 结构：`{(group_id, sender): [套1, 套2, ...]}` — 同一人可以有多套证件
- 行驶证/身份证正面：按 `owner_name` / `name` 匹配已有套，不匹配则创建新套
- 身份证反面（无姓名）：归入最近一套有身份证正面但没有反面的条目
- 每套独立检查齐全/超时，独立提交
- 文本消息：用已识别的姓名/车牌去清洗后的文本做包含匹配

- [ ] **Step 1: 实现 matcher.py**

```python
# quote/matcher.py
"""
证件配对器
按 (group_id, sender) 维度管理多套证件数据。
同一人发多台车的证件时，按姓名自动拆分为不同套。
身份证反面就近归入有正面但无反面的套。
超时1分钟后自动提交 pre-quote。
"""

import re
import time
import logging
import threading
import requests
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://riskapi.wuhuxiche.com"

# 文本清洗：去除空格、横杠、下划线等特殊字符
_CLEAN_PATTERN = re.compile(r'[\s\-_—–·.。,，、;；:：!！?？()\[\]【】《》""\'\'`~\u3000]+')


def _clean_text(text: str) -> str:
    """清洗文本，去除特殊字符，用于匹配"""
    return _CLEAN_PATTERN.sub("", text)


def _new_entry(binding: dict, group_id: str, sender: str) -> dict:
    """创建一套新的待处理条目"""
    return {
        "start_time": time.time(),
        "vehicle_info": {},
        "owner_info": {},
        "raw_texts": [],
        "extra_info": "",
        "binding": binding,
        "names": set(),       # 已识别的姓名集合（用于匹配文本）
        "plates": set(),      # 已识别的车牌集合（用于匹配文本）
        "group_id": group_id,
        "sender": sender,
        "wechat_name": binding.get("wechat_name", ""),
        "group_name": binding.get("group_name", ""),
        "has_idcard_front": False,  # 是否已有身份证正面
        "has_idcard_back": False,   # 是否已有身份证反面
    }


class QuoteMatcher:
    """证件配对器"""

    def __init__(self, timeout_seconds: int = 60, check_interval: int = 10):
        # key: (group_id, sender), value: list of entry dicts
        self._pending: Dict[tuple, List[dict]] = {}
        self._lock = threading.Lock()
        self._timeout = timeout_seconds
        self._check_interval = check_interval
        # 启动超时检查线程
        self._timer_thread = threading.Thread(target=self._timeout_loop, daemon=True)
        self._timer_thread.start()

    def add_vehicle_license(self, group_id: str, sender: str, ocr_data: dict,
                            cos_url: str, binding: dict):
        """添加行驶证识别结果"""
        with self._lock:
            key = (group_id, sender)
            owner_name = ocr_data.get("owner_name", "")
            plate = ocr_data.get("plate_number", "")

            # 按姓名查找已有套
            entry = self._find_entry_by_name(key, owner_name) if owner_name else None

            # 没找到匹配的套 → 创建新套
            if entry is None:
                entry = _new_entry(binding, group_id, sender)
                self._pending.setdefault(key, []).append(entry)

            entry["vehicle_info"] = {
                "plate_number": plate,
                "vin": ocr_data.get("vin", ""),
                "engine_number": ocr_data.get("engine_number", ""),
                "first_register_date": ocr_data.get("first_register_date", ""),
                "model": ocr_data.get("model", ""),
            }
            # 行驶证图片 URL 暂存（不提交到 pre-quote，但日志用）
            entry["license_image_cos_url"] = cos_url
            if owner_name:
                entry["names"].add(owner_name)
            if plate:
                entry["plates"].add(plate)
            self._try_match_texts(entry)
            logger.info("配对器：添加行驶证 key=%s, 车牌=%s, 车主=%s", key, plate, owner_name)
            self._check_complete(key, entry)

    def add_idcard_front(self, group_id: str, sender: str, ocr_data: dict,
                         cos_url: str, binding: dict):
        """添加身份证正面识别结果"""
        with self._lock:
            key = (group_id, sender)
            name = ocr_data.get("name", "")

            # 按姓名查找已有套
            entry = self._find_entry_by_name(key, name) if name else None

            if entry is None:
                entry = _new_entry(binding, group_id, sender)
                self._pending.setdefault(key, []).append(entry)

            entry["owner_info"]["name"] = name
            entry["owner_info"]["id_number"] = ocr_data.get("id_number", "")
            entry["owner_info"]["id_card_image_cos_url"] = cos_url
            entry["has_idcard_front"] = True
            if name:
                entry["names"].add(name)
            self._try_match_texts(entry)
            logger.info("配对器：添加身份证正面 key=%s, 姓名=%s", key, name)
            self._check_complete(key, entry)

    def add_idcard_back(self, group_id: str, sender: str, ocr_data: dict,
                        cos_url: str, binding: dict):
        """
        添加身份证反面识别结果
        反面没有姓名，归入最近一套有正面但没有反面的条目
        """
        with self._lock:
            key = (group_id, sender)
            entries = self._pending.get(key, [])

            # 查找有正面但没反面的套（从后往前找，取最近的）
            entry = None
            for e in reversed(entries):
                if e["has_idcard_front"] and not e["has_idcard_back"]:
                    entry = e
                    break

            # 没找到有正面的套，归入最后一套；如果没有任何套则新建
            if entry is None:
                if entries:
                    entry = entries[-1]
                else:
                    entry = _new_entry(binding, group_id, sender)
                    self._pending.setdefault(key, []).append(entry)

            entry["has_idcard_back"] = True
            # 反面数据暂存（不提交到 pre-quote 的必填字段，但存着备用）
            entry["_idcard_back_data"] = {
                "id_card_start_date": ocr_data.get("id_card_start_date", ""),
                "id_card_end_date": ocr_data.get("id_card_end_date", ""),
                "opposite_img": cos_url,
            }
            logger.info("配对器：添加身份证反面 key=%s", key)
            self._check_complete(key, entry)

    def add_text(self, group_id: str, sender: str, text: str, binding: dict):
        """添加文本消息"""
        with self._lock:
            key = (group_id, sender)
            entries = self._pending.get(key, [])

            if not entries:
                # 还没有任何证件，创建一个空套暂存文本
                entry = _new_entry(binding, group_id, sender)
                self._pending.setdefault(key, []).append(entry)
                entry["raw_texts"].append(text)
            else:
                # 尝试匹配到具体某一套
                matched = False
                for entry in entries:
                    if self._text_matches_entry(text, entry):
                        existing = entry["extra_info"]
                        entry["extra_info"] = f"{existing}；{text}" if existing else text
                        matched = True
                        logger.info("配对器：文本匹配到套 owner=%s, text=%s",
                                   entry["owner_info"].get("name", ""), text[:50])
                        break
                if not matched:
                    # 没匹配到，存入最后一套的 raw_texts
                    entries[-1]["raw_texts"].append(text)

            logger.info("配对器：添加文本 key=%s, text=%s", key, text[:50])

    def _find_entry_by_name(self, key: tuple, name: str) -> Optional[dict]:
        """在已有套中查找姓名匹配的条目"""
        if not name:
            return None
        entries = self._pending.get(key, [])
        for entry in entries:
            if name in entry["names"]:
                return entry
        return None

    def _text_matches_entry(self, text: str, entry: dict) -> bool:
        """检查文本是否匹配某一套的姓名或车牌"""
        if not entry["names"] and not entry["plates"]:
            return False
        cleaned = _clean_text(text)
        for name in entry["names"]:
            if name and name in cleaned:
                return True
        for plate in entry["plates"]:
            if plate and _clean_text(plate) in cleaned:
                return True
        return False

    def _try_match_texts(self, entry: dict):
        """用已识别的姓名和车牌去匹配暂存的文本"""
        if not entry["raw_texts"]:
            return
        if not entry["names"] and not entry["plates"]:
            return

        matched_texts = []
        remaining_texts = []

        for text in entry["raw_texts"]:
            if self._text_matches_entry(text, entry):
                matched_texts.append(text)
            else:
                remaining_texts.append(text)

        if matched_texts:
            existing = entry["extra_info"]
            new_text = "；".join(matched_texts)
            entry["extra_info"] = f"{existing}；{new_text}" if existing else new_text
            entry["raw_texts"] = remaining_texts
            logger.info("配对器：文本匹配成功，extra_info=%s", entry["extra_info"])

    def _check_complete(self, key: tuple, entry: dict):
        """检查证件是否齐全（行驶证+身份证正面），齐全则立即提交"""
        has_vehicle = bool(entry["vehicle_info"].get("plate_number") or
                          entry["vehicle_info"].get("vin"))
        has_owner = bool(entry["owner_info"].get("name") or
                        entry["owner_info"].get("id_number"))
        if has_vehicle and has_owner:
            logger.info("配对器：证件齐全，立即提交 key=%s, owner=%s",
                       key, entry["owner_info"].get("name", ""))
            self._submit_entry(key, entry)

    def _timeout_loop(self):
        """定时检查超时条目"""
        while True:
            time.sleep(self._check_interval)
            now = time.time()
            to_submit = []
            with self._lock:
                for key, entries in list(self._pending.items()):
                    for entry in list(entries):
                        if now - entry["start_time"] >= self._timeout:
                            to_submit.append((key, entry))
            for key, entry in to_submit:
                with self._lock:
                    entries = self._pending.get(key, [])
                    if entry in entries:
                        logger.info("配对器：超时提交 key=%s, owner=%s",
                                   key, entry["owner_info"].get("name", ""))
                        self._submit_entry(key, entry)

    def _submit_entry(self, key: tuple, entry: dict):
        """
        提交单套证件到 pre-quote 接口
        注意：调用时必须持有 self._lock
        """
        # 从 pending 列表中移除该套
        entries = self._pending.get(key, [])
        if entry in entries:
            entries.remove(entry)
        if not entries:
            self._pending.pop(key, None)

        # 未匹配的文本也合并到 extra_info
        if entry["raw_texts"]:
            remaining = "；".join(entry["raw_texts"])
            existing = entry["extra_info"]
            entry["extra_info"] = f"{existing}；{remaining}" if existing else remaining

        # 构建请求体（只传 pre-quote 接口需要的字段）
        item = {
            "vehicle_info": {
                "plate_number": entry["vehicle_info"].get("plate_number", ""),
                "vin": entry["vehicle_info"].get("vin", ""),
                "engine_number": entry["vehicle_info"].get("engine_number", ""),
                "first_register_date": entry["vehicle_info"].get("first_register_date", ""),
                "model": entry["vehicle_info"].get("model", ""),
            },
            "owner_info": {
                "name": entry["owner_info"].get("name", ""),
                "id_number": entry["owner_info"].get("id_number", ""),
                "id_card_image_cos_url": entry["owner_info"].get("id_card_image_cos_url", ""),
            },
            "wechat_id": entry["sender"],
            "wechat_name": entry.get("wechat_name", ""),
            "group_id": entry["group_id"],
            "group_name": entry.get("group_name", ""),
            "extra_info": entry["extra_info"],
        }

        threading.Thread(
            target=self._do_submit,
            args=(item,),
            daemon=True,
        ).start()

    def _do_submit(self, item: dict):
        """实际提交请求"""
        try:
            resp = requests.post(
                f"{BASE_URL}/insurance/pre-quote",
                json={"items": [item]},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") == 0:
                data = result.get("data", {})
                results = data.get("results", [])
                for r in results:
                    logger.info("预报价提交成功: state=%s(%s), order_id=%s, "
                               "auto_quote=%s, missing=%s",
                               r.get("state"), r.get("state_text"),
                               r.get("order_info", {}).get("order_id"),
                               r.get("auto_quote_triggered"),
                               r.get("missing_fields", []))
            else:
                logger.error("预报价提交失败: %s", result.get("msg", "未知错误"))
        except Exception as e:
            logger.error("预报价提交异常: %s", e)
```

- [ ] **Step 2: Commit**

```bash
git add quote/matcher.py
git commit -m "feat(quote): 添加多套证件配对器，支持姓名拆分、反面就近归组、超时提交"
```

---

### Task 4: 创建 quote/handler.py — 主处理逻辑

**Files:**
- Create: `quote/handler.py`

- [ ] **Step 1: 实现 handler.py**

```python
# quote/handler.py
"""
保险报价处理器
监听图片和文本消息，通过百度OCR识别行驶证/身份证，配对后提交预报价
"""

import gc
import logging
import threading
import time
from queue import Queue, Full

from quote.binding import BindingService
from quote.ocr import QuoteOCR
from quote.matcher import QuoteMatcher

logger = logging.getLogger(__name__)


class QuoteHandler:
    """保险报价处理器"""

    def __init__(self, cos_storage=None, app_config: dict = None):
        self._queue = Queue(maxsize=200)
        self.cos_storage = cos_storage
        self.finance_sdk = None   # 由 main.py 注入
        self.sdk_config = None    # 由 main.py 注入

        # 初始化子组件
        self.binding_service = BindingService()
        self.matcher = QuoteMatcher(timeout_seconds=60, check_interval=10)

        # 初始化百度 OCR（复用 config.yaml 中的 baidu_ocr 配置）
        self._ocr = None
        if app_config:
            baidu_cfg = app_config.get("baidu_ocr", {})
            api_key = baidu_cfg.get("api_key", "")
            secret_key = baidu_cfg.get("secret_key", "")
            if api_key and secret_key:
                self._ocr = QuoteOCR(api_key, secret_key)
                logger.info("报价OCR初始化完成")
            else:
                logger.warning("报价OCR未配置百度OCR密钥，证件识别不可用")

        # 启动消费线程
        self._consumer_thread = threading.Thread(target=self._consume, daemon=True)
        self._consumer_thread.start()
        logger.info("保险报价处理器已启动")

    def check_and_enqueue(self, msg_data: dict):
        """
        检查消息是否需要处理，是则入队。
        由 fetcher._check_quote_trigger 调用。
        """
        roomid = msg_data.get("roomid", "")
        sender = msg_data.get("sender", "")

        if not roomid:
            return

        binding = self.binding_service.find_binding(roomid, sender)
        if not binding:
            return

        msg_data["binding"] = binding
        try:
            self._queue.put_nowait(msg_data)
        except Full:
            logger.warning("报价队列已满，丢弃消息 seq=%s", msg_data.get("seq"))

    def _consume(self):
        """消费线程主循环"""
        while True:
            try:
                msg = self._queue.get(block=True, timeout=1)
            except Exception:
                continue
            try:
                self._process(msg)
            except Exception as e:
                logger.error("报价处理异常: %s", e, exc_info=True)

    def _process(self, msg: dict):
        """处理单条消息"""
        msgtype = msg.get("msgtype", "")
        if msgtype == "image":
            self._process_image(msg)
        elif msgtype == "text":
            self._process_text(msg)

    def _process_image(self, msg: dict):
        """处理图片消息：下载→COS上传→OCR识别→配对"""
        roomid = msg["roomid"]
        sender = msg["sender"]
        binding = msg["binding"]
        content = msg.get("content", {})
        sdkfileid = content.get("sdkfileid", "")
        seq = msg.get("seq", 0)

        if not sdkfileid:
            logger.warning("报价：图片消息缺少 sdkfileid, seq=%d", seq)
            return

        if not self._ocr:
            logger.warning("报价：OCR 未初始化，跳过 seq=%d", seq)
            return

        # 1. 通过 SDK 下载图片到内存
        image_bytes = None
        try:
            if self.finance_sdk and self.sdk_config:
                image_bytes = self.finance_sdk.get_media_data_bytes(
                    sdkfileid,
                    proxy=self.sdk_config.get("proxy", ""),
                    passwd=self.sdk_config.get("proxy_passwd", ""),
                    timeout=self.sdk_config.get("timeout", 10),
                )
        except Exception as e:
            logger.error("报价：下载图片失败 seq=%d: %s", seq, e)
            return

        if not image_bytes:
            logger.error("报价：下载图片为空 seq=%d", seq)
            return

        try:
            # 2. 上传 COS
            cos_url = ""
            if self.cos_storage:
                timestamp = int(time.time() * 1000)
                cos_key = f"quote/{roomid}/{sender}/{timestamp}.jpg"
                cos_url = self.cos_storage.upload_bytes(image_bytes, cos_key)

            # 3. 同时尝试行驶证和身份证识别
            vehicle_result = self._ocr.recognize_vehicle_license(image_bytes)
            idcard_result = self._ocr.recognize_idcard(image_bytes)

            vehicle_ok = vehicle_result.get("success", False)
            idcard_ok = idcard_result.get("success", False)

            if vehicle_ok:
                logger.info("报价：行驶证识别成功 seq=%d, 车牌=%s",
                           seq, vehicle_result["data"].get("plate_number"))
                self.matcher.add_vehicle_license(
                    roomid, sender, vehicle_result["data"], cos_url, binding)
            elif idcard_ok:
                side = idcard_result.get("side", "front")
                logger.info("报价：身份证识别成功 seq=%d, side=%s", seq, side)
                if side == "front":
                    self.matcher.add_idcard_front(
                        roomid, sender, idcard_result["data"], cos_url, binding)
                else:
                    self.matcher.add_idcard_back(
                        roomid, sender, idcard_result["data"], cos_url, binding)
            else:
                # 两个都识别不出来，打印图片 COS 地址
                logger.warning("报价：图片识别失败 seq=%d, cos_url=%s, "
                             "行驶证错误=%s, 身份证错误=%s",
                             seq, cos_url,
                             vehicle_result.get("error", ""),
                             idcard_result.get("error", ""))
        finally:
            del image_bytes
            gc.collect()

    def _process_text(self, msg: dict):
        """处理文本消息：暂存等待配对"""
        roomid = msg["roomid"]
        sender = msg["sender"]
        binding = msg["binding"]
        content = msg.get("content", {})
        text = content.get("content", "")

        if not text or not text.strip():
            return

        logger.info("报价：收到文本消息 room=%s, sender=%s, text=%s",
                    roomid, sender, text[:50])
        self.matcher.add_text(roomid, sender, text, binding)
```

- [ ] **Step 2: Commit**

```bash
git add quote/handler.py
git commit -m "feat(quote): 添加报价处理器，图片下载→COS→OCR→配对完整流水线"
```

---

### Task 5: 修改 fetcher.py — 新增报价触发点

**Files:**
- Modify: `core/fetcher.py`

- [ ] **Step 1: 在 `_process_item` 方法中新增调用**

在 `core/fetcher.py:188` 行 `self._check_insurance_trigger(msg_seq, msg_data, parsed)` 之后添加：

```python
            # 检查是否需要触发保险报价
            self._check_quote_trigger(msg_seq, msg_data, parsed)
```

- [ ] **Step 2: 在文件末尾新增方法**

```python
    def _check_quote_trigger(self, seq: int, msg_data: dict, parsed: dict):
        """检查是否需要触发保险报价（图片/文本消息）"""
        handler = getattr(self, "quote_handler", None)
        if not handler:
            return

        msgtype = parsed.get("msgtype", "")
        roomid = parsed.get("roomid", "")
        sender = parsed.get("from", "")
        content = parsed.get("content", {})

        # 仅处理图片和文本消息
        if msgtype not in ("image", "text"):
            return

        # 仅处理群聊消息
        if not roomid:
            return

        handler.check_and_enqueue({
            "msgtype": msgtype,
            "roomid": roomid,
            "sender": sender,
            "content": content,
            "seq": seq,
        })
```

- [ ] **Step 3: Commit**

```bash
git add core/fetcher.py
git commit -m "feat(quote): fetcher新增报价触发点，监听图片和文本消息"
```

---

### Task 6: 修改 main.py — 初始化报价模块

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 添加 import（第24行附近）**

```python
from quote.handler import QuoteHandler
```

- [ ] **Step 2: 在 insurance 初始化之后创建 QuoteHandler（第113行附近）**

```python
    # 初始化保险报价模块
    quote_handler = QuoteHandler(
        cos_storage=cos_storage,
        app_config=config,
    )
```

- [ ] **Step 3: SDK 初始化成功后绑定（第222行 `fetcher.insurance_handler = insurance_handler` 之后）**

```python
        # 将 quote_handler 绑定到 fetcher（用于自动触发报价）
        fetcher.quote_handler = quote_handler
        quote_handler.finance_sdk = finance_sdk
        quote_handler.sdk_config = sdk_config
```

- [ ] **Step 4: 添加到 app.extensions（第229行附近）**

```python
            "quote_handler": quote_handler,
```

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(quote): main.py初始化报价模块并绑定到fetcher"
```

---

## 数据流总结

```
消息进入 fetcher._process_item()
  → _check_quote_trigger()
    → msgtype == "image" 或 "text" 且是群聊
      → quote_handler.check_and_enqueue()
        → binding_service.find_binding(roomid, sender) 匹配监控列表
          → 不在列表：丢弃
          → 在列表：入队
            → 消费线程 _process()
              → image: SDK下载 → COS上传(quote/{roomid}/{sender}/{ts}.jpg)
                      → 行驶证OCR + 身份证OCR 双尝试
                        → 行驶证成功：matcher.add_vehicle_license（按owner_name归套）
                        → 身份证正面：matcher.add_idcard_front（按name归套）
                        → 身份证反面：matcher.add_idcard_back（就近归入有正面无反面的套）
                        → 都失败：log.warning 打印 COS URL
              → text: matcher.add_text（用姓名/车牌匹配清洗后文本归套）
            → matcher 配对检查（每套独立）：
              → 行驶证+身份证正面齐全 → 立即提交 pre-quote
              → 超时1分钟 → 自动提交 pre-quote（不管是否齐全）

多套证件拆分规则：
  同一 (group_id, sender) 下按姓名拆分：
    - 行驶证的"所有人"姓名 匹配 → 归入已有套
    - 身份证正面的"姓名" 匹配 → 归入已有套
    - 身份证反面（无姓名） → 归入最近一套有正面无反面的
    - 姓名不匹配任何已有套 → 创建新套
```
