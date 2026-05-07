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


# 车牌号正则：省份汉字 + 城市字母 + 5位车牌码（含新能源）
_PLATE_PATTERN = re.compile(
    r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁夏]'
    r'[A-Z][0-9A-Z·•]{4,5}'
)
# VIN码正则：17位，不含 I/O/Q
_VIN_PATTERN = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')
# 保单号正则：字母开头、8-20位字母数字组合（排除纯数字，避免误匹配电话/日期）
_POLICY_PATTERN = re.compile(r'[A-Z][A-Z0-9]{7,19}')


def _extract_plate(text: str) -> str:
    m = _PLATE_PATTERN.search(text)
    if m:
        return re.sub(r'[·•]', '', m.group(0))
    return ""


def _extract_vin(text: str) -> str:
    m = _VIN_PATTERN.search(text.upper())
    return m.group(0) if m else ""


def _extract_policy_number(text: str, exclude_vin: str = "") -> str:
    """从文本中提取保单号（字母开头的8-20位字母数字串，排除VIN）"""
    upper = text.upper()
    for m in _POLICY_PATTERN.finditer(upper):
        candidate = m.group(0)
        # VIN恰好17位且符合VIN字符集，跳过
        if candidate == exclude_vin.upper():
            continue
        if len(candidate) == 17 and _VIN_PATTERN.fullmatch(candidate):
            continue
        return candidate
    return ""


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
        "names": set(),
        "plates": set(),
        "group_id": group_id,
        "sender": sender,
        "wechat_name": binding.get("wechat_name", ""),
        "group_name": binding.get("group_name", ""),
        "has_idcard_front": False,
        "has_idcard_back": False,
    }


class QuoteMatcher:
    """证件配对器"""

    def __init__(self, timeout_seconds: int = 60, check_interval: int = 10):
        self._pending: Dict[tuple, List[dict]] = {}
        self._pending_backs: Dict[tuple, List[dict]] = {}  # 暂存无归属的身份证反面
        self._lock = threading.Lock()
        self._timeout = timeout_seconds
        self._check_interval = check_interval
        self._timer_thread = threading.Thread(target=self._timeout_loop, daemon=True)
        self._timer_thread.start()

    def add_vehicle_license(self, group_id: str, sender: str, ocr_data: dict,
                            cos_url: str, binding: dict):
        """添加行驶证识别结果"""
        with self._lock:
            key = (group_id, sender)
            owner_name = ocr_data.get("owner_name", "")
            plate = ocr_data.get("plate_number", "")

            entry = self._find_entry_by_name(key, owner_name) if owner_name else None

            if entry is None:
                entry = _new_entry(binding, group_id, sender)
                self._pending.setdefault(key, []).append(entry)

            entry["vehicle_info"] = {
                "plate_number": plate,
                "vin": ocr_data.get("vin", ""),
                "engine_number": ocr_data.get("engine_number", ""),
                "first_register_date": ocr_data.get("first_register_date", ""),
                "issue_date": ocr_data.get("issue_date", ""),
                "model": ocr_data.get("model", ""),
            }
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
            # 归入暂存的身份证反面
            self._attach_pending_back(key, entry)
            self._try_match_texts(entry)
            logger.info("配对器：添加身份证正面 key=%s, 姓名=%s", key, name)
            self._check_complete(key, entry)

    def add_idcard_back(self, group_id: str, sender: str, ocr_data: dict,
                        cos_url: str, binding: dict):
        """添加身份证反面识别结果，归入最近一套有正面但没反面的条目"""
        with self._lock:
            key = (group_id, sender)
            entries = self._pending.get(key, [])

            entry = None
            for e in reversed(entries):
                if e["has_idcard_front"] and not e["has_idcard_back"]:
                    entry = e
                    break

            if entry is None:
                if entries:
                    entry = entries[-1]
                else:
                    # 没有任何现有条目，暂存反面数据等后续证件到达时归入
                    back_data = {
                        "id_card_start_date": ocr_data.get("id_card_start_date", ""),
                        "id_card_end_date": ocr_data.get("id_card_end_date", ""),
                        "opposite_img": cos_url,
                    }
                    self._pending_backs.setdefault(key, []).append(back_data)
                    logger.info("配对器：暂存身份证反面（无现有条目） key=%s", key)
                    return

            entry["has_idcard_back"] = True
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
                entry = _new_entry(binding, group_id, sender)
                self._pending.setdefault(key, []).append(entry)
                entry["raw_texts"].append(text)
            else:
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
                    entries[-1]["raw_texts"].append(text)

            logger.info("配对器：添加文本 key=%s, text=%s", key, text[:50])

    def _attach_pending_back(self, key: tuple, entry: dict):
        """将暂存的身份证反面数据归入当前条目（调用时需持有 self._lock）"""
        backs = self._pending_backs.get(key)
        if backs and not entry["has_idcard_back"]:
            back_data = backs.pop(0)
            if not backs:
                del self._pending_backs[key]
            entry["has_idcard_back"] = True
            entry["_idcard_back_data"] = back_data
            logger.info("配对器：归入暂存的身份证反面 key=%s", key)

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
        """提交单套证件到 pre-quote 接口（调用时必须持有 self._lock）"""
        entries = self._pending.get(key, [])
        if entry in entries:
            entries.remove(entry)
        if not entries:
            self._pending.pop(key, None)

        if entry["raw_texts"]:
            remaining = "；".join(entry["raw_texts"])
            existing = entry["extra_info"]
            entry["extra_info"] = f"{existing}；{remaining}" if existing else remaining

        item = {
            "vehicle_info": {
                "plate_number": entry["vehicle_info"].get("plate_number", ""),
                "vin": entry["vehicle_info"].get("vin", ""),
                "engine_number": entry["vehicle_info"].get("engine_number", ""),
                "first_register_date": entry["vehicle_info"].get("first_register_date", ""),
                "issue_date": entry["vehicle_info"].get("issue_date", ""),
                "model": entry["vehicle_info"].get("model", ""),
                "license_image_cos_url": entry.get("license_image_cos_url", ""),
            },
            "owner_info": {
                "name": entry["owner_info"].get("name", ""),
                "id_number": entry["owner_info"].get("id_number", ""),
                "id_card_image_cos_url": entry["owner_info"].get("id_card_image_cos_url", ""),
                "id_card_start_date": entry.get("_idcard_back_data", {}).get("id_card_start_date", ""),
                "id_card_end_date": entry.get("_idcard_back_data", {}).get("id_card_end_date", ""),
                "id_card_back_image_cos_url": entry.get("_idcard_back_data", {}).get("opposite_img", ""),
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

    def check_and_submit_renewal(self, group_id: str, sender: str, text: str, binding: dict):
        """处理含"续保"关键词的文本消息，提取车牌/VIN/保单号后提交续保报价接口"""
        plate = _extract_plate(text)
        vin = _extract_vin(text)
        policy_number = _extract_policy_number(text, exclude_vin=vin)

        if not plate and not vin and not policy_number:
            logger.warning("续保：文本中未识别到车牌、VIN或保单号，跳过提交 "
                           "group_id=%s sender=%s text=%s", group_id, sender, text[:80])
            return

        item = {
            "plate_number": plate,
            "vin": vin,
            "policy_number": policy_number,
            "wechat_id": sender,
            "wechat_name": binding.get("wechat_name", ""),
            "group_id": group_id,
            "group_name": binding.get("group_name", ""),
            "extra_info": text,
        }
        logger.info("续保：识别到续保请求 plate=%s vin=%s policy_number=%s group_id=%s sender=%s",
                    plate, vin, policy_number, group_id, sender)
        threading.Thread(target=self._do_submit_renewal, args=(item,), daemon=True).start()

    def _do_submit_renewal(self, item: dict):
        """提交续保报价请求"""
        try:
            resp = requests.post(
                f"{BASE_URL}/insurance/renewal-quote",
                json={"items": [item]},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") == 0:
                data = result.get("data", {})
                results = data.get("results", [])
                for r in results:
                    logger.info("续保报价提交成功: state=%s, order_id=%s, auto_quote=%s",
                                r.get("state"),
                                r.get("order_info", {}).get("order_id"),
                                r.get("auto_quote_triggered"))
            else:
                logger.error("续保报价提交失败: %s", result.get("msg", "未知错误"))
        except Exception as e:
            logger.error("续保报价提交异常: %s", e)
