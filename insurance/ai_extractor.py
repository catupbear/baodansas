#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI质检模块 — 多模态大模型提取器
PDF → pypdfium2 渲染图片 → OpenAI 兼容多模态 API → JSON 字段。

- 所有国产主流多模态模型（通义/智谱/豆包/Kimi/混元）均兼容 OpenAI Chat Completions 协议，
  仅通过 base_url + model + api_key 切换厂商，不引入任何厂商专属 SDK（requests 裸调）。
- 多模型按优先级自动降级：当前模型失败/超时 → 依次尝试下一个启用的模型。
"""

import base64
import io
import json
import logging
import re
import time

import requests

from .ai_db import get_active_model, get_fallback_models, get_ai_model_config

logger = logging.getLogger(__name__)

# 图片渲染参数：长边不超过该像素，控制 token 消耗
_MAX_IMAGE_SIDE = 1600
_RENDER_SCALE = 2.0  # 基础缩放（72dpi * 2 = 144dpi），渲染后再按长边压缩

DEFAULT_TIMEOUT = 60

# 原始返回值落库/回传前端的最大长度，避免个别模型返回超长内容占用过多存储
_MAX_RAW_RESPONSE_LEN = 50000


class AIExtractError(Exception):
    """AI 提取失败（所有模型都不可用/解析失败）。"""


def render_pdf_to_images(pdf_bytes: bytes, max_pages: int = 4) -> list:
    """
    将 PDF 前 N 页渲染为 JPEG bytes 列表。
    依赖 pypdfium2（纯 pip 依赖，无系统组件）；未安装时抛出明确错误。
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise AIExtractError("服务器未安装 pypdfium2，无法渲染 PDF 图片（pip install pypdfium2）")
    try:
        from PIL import Image  # noqa: F401  pypdfium2 渲染输出依赖 Pillow
    except ImportError:
        raise AIExtractError("服务器未安装 Pillow，无法处理图片（pip install Pillow）")

    images = []
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page_count = min(len(doc), max(1, max_pages))
        for i in range(page_count):
            page = doc[i]
            bitmap = page.render(scale=_RENDER_SCALE)
            pil_img = bitmap.to_pil()
            # 长边压缩
            w, h = pil_img.size
            long_side = max(w, h)
            if long_side > _MAX_IMAGE_SIDE:
                ratio = _MAX_IMAGE_SIDE / long_side
                pil_img = pil_img.resize((int(w * ratio), int(h * ratio)))
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=82)
            images.append(buf.getvalue())
            page.close()
    finally:
        doc.close()
    if not images:
        raise AIExtractError("PDF 渲染结果为空")
    return images


def build_prompt(template: str) -> str:
    """将模板中的 {{FIELD_LIST}} 替换为提取字段清单。"""
    from .policy_parser import FIELD_ORDER
    field_list = "、".join(FIELD_ORDER)
    return (template or "").replace("{{FIELD_LIST}}", field_list)


def _parse_json_answer(text: str) -> dict:
    """从模型回复中解析 JSON 对象（容忍 markdown 代码块包裹/前后杂文）。"""
    text = (text or "").strip()
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        # 截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def normalize_fields(raw: dict) -> dict:
    """归一化模型输出：去掉 null/空值，全部转字符串并去首尾空白。"""
    fields = {}
    if not isinstance(raw, dict):
        return fields
    for k, v in raw.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        s = str(v).strip()
        if not s or s.lower() in ("null", "none", "无", "未识别"):
            continue
        fields[str(k).strip()] = s
    return fields


def call_vision_model(model_cfg: dict, prompt: str, images: list) -> dict:
    """
    调用单个 OpenAI 兼容多模态模型。
    返回 {"fields": dict, "usage": {...}, "raw": str}；失败抛异常。
    """
    base_url = (model_cfg.get("base_url") or "").rstrip("/")
    model = model_cfg.get("model") or ""
    api_key = model_cfg.get("api_key") or ""
    timeout = int(model_cfg.get("timeout") or DEFAULT_TIMEOUT)
    if not base_url or not model or not api_key:
        raise AIExtractError("模型配置不完整（base_url/model/api_key）")

    content = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise AIExtractError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise AIExtractError(f"模型无返回内容: {json.dumps(data, ensure_ascii=False)[:300]}")
    answer = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    # 完整原始响应文本（未解析的 HTTP body），供前端「查看原始返回」按钮展示，
    # 便于排查模型输出异常/JSON解析失败等问题；按长度截断避免占用过多存储
    raw_response = resp.text[:_MAX_RAW_RESPONSE_LEN] if resp.text else ""
    try:
        fields = normalize_fields(_parse_json_answer(answer))
    except (json.JSONDecodeError, ValueError) as e:
        raise AIExtractError(f"模型输出非合法JSON: {e}; 原文: {answer[:300]}")
    return {"fields": fields, "usage": usage, "raw": answer, "raw_response": raw_response}


def extract_fields_from_pdf(db, pdf_bytes: bytes) -> dict:
    """
    完整提取流程（多模型自动降级）。

    Returns:
        {
          "fields": dict,          # 归一化后的字段
          "model": str,            # 实际使用的模型名（name/model）
          "prompt_tokens": int,
          "completion_tokens": int,
          "duration_ms": int,
          "fallback_used": bool,   # 是否发生了降级
          "errors": [str],         # 降级过程中每个失败模型的错误
          "raw_response": str,     # 最终成功那次调用的完整原始响应文本（未解析）
        }
    失败抛 AIExtractError。
    """
    cfg = get_ai_model_config(db)
    if not cfg.get("enabled"):
        raise AIExtractError("AI 能力未启用（请在系统设置 → AI 模型中开启）")

    active = get_active_model(cfg)
    if not active:
        raise AIExtractError("未配置可用的 AI 模型")

    prompt = build_prompt(cfg.get("prompt_template") or "")
    images = render_pdf_to_images(pdf_bytes, int(cfg.get("max_pages") or 4))

    candidates = [active] + get_fallback_models(cfg, exclude_id=active.get("id", ""))
    start = time.time()
    errors = []
    for idx, m in enumerate(candidates):
        try:
            result = call_vision_model(m, prompt, images)
            usage = result.get("usage") or {}
            return {
                "fields": result["fields"],
                "model": f"{m.get('name', '')}/{m.get('model', '')}".strip("/"),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "duration_ms": int((time.time() - start) * 1000),
                "fallback_used": idx > 0,
                "errors": errors,
                "raw_response": result.get("raw_response", ""),
            }
        except Exception as e:
            err = f"[{m.get('name') or m.get('model')}] {e}"
            errors.append(err)
            logger.warning("AI模型调用失败，尝试降级: %s", err)

    raise AIExtractError("所有模型均调用失败: " + " | ".join(errors)[:800])


def test_model_connection(model_cfg: dict) -> dict:
    """
    测试单个模型连通性：发一次极小的纯文本请求。
    返回 {"ok": bool, "duration_ms": int, "error": str}
    """
    base_url = (model_cfg.get("base_url") or "").rstrip("/")
    model = model_cfg.get("model") or ""
    api_key = model_cfg.get("api_key") or ""
    if not base_url or not model or not api_key:
        return {"ok": False, "duration_ms": 0, "error": "模型配置不完整（base_url/model/api_key）"}
    start = time.time()
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "回复：OK"}],
                "max_tokens": 8,
            },
            timeout=int(model_cfg.get("timeout") or 30),
        )
        duration = int((time.time() - start) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "duration_ms": duration, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        if not data.get("choices"):
            return {"ok": False, "duration_ms": duration, "error": f"无返回内容: {json.dumps(data, ensure_ascii=False)[:200]}"}
        return {"ok": True, "duration_ms": duration, "error": ""}
    except Exception as e:
        return {"ok": False, "duration_ms": int((time.time() - start) * 1000), "error": str(e)[:300]}


def download_pdf_from_cos(record: dict) -> bytes:
    """从记录的 cos_url 下载 PDF 内容（AI提取/质检共用）。"""
    cos_url = (record or {}).get("cos_url") or ""
    if not cos_url:
        raise AIExtractError("记录无 COS 文件地址，无法进行 AI 提取")
    resp = requests.get(cos_url, timeout=30)
    if resp.status_code != 200:
        raise AIExtractError(f"COS 文件下载失败: HTTP {resp.status_code}")
    if not resp.content.startswith(b"%PDF"):
        raise AIExtractError("COS 文件不是有效的 PDF")
    return resp.content
