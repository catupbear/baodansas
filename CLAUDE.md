# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

企业微信会话存档系统（wxbot）—— 基于 Flask 的消息拉取、解密、存储与查询平台。通过企业微信 Finance SDK（C 动态库）拉取加密消息，经 RSA + AES 两阶段解密后存入 MySQL，并提供 Web API 和前端查询界面。集成保单识别模块，支持指定群 PDF 自动识别并同步到钉钉多维表格。

> 📂 项目目录结构详见 [docs/project-structure.md](docs/project-structure.md)

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务（默认端口 8090，由 config.yaml 控制）
python main.py
```

无测试框架、无 lint 配置、无 Makefile。

## 架构

### 消息处理流水线

```
POST /callback (msgaudit_notify)
  → 后台线程 fetcher.fetch_new_messages()
    → SDK get_chat_data(seq, limit)
      → RSA 解密 encrypt_random_key → AES 解密 encrypt_chat_msg
        → parser.parse() 解析 25+ 消息类型
          → db.save_message() 写入 MySQL
            → 更新 seq 游标
            → 检查是否触发保单识别（PDF + 监控群）
              → insurance_handler.enqueue() 入队
```

### 保单识别流水线

```
Queue 串行消费（控制内存）
  → SDK 下载 PDF 到内存（>20MB 走临时文件）
  → 上传 COS（路径：insurance/{roomid}/{sender}/{filename}）
  → pdfplumber 提取文本 → 失败降级百度 OCR
  → policy_parser 解析 50+ 保险公司的保单字段
  → field_mapping 字段映射
  → 自动同步钉钉多维表格（可配置）
  → 记录结果到 insurance_records 表
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `main.py` | 入口，加载配置、初始化 Flask app 及所有组件 |
| `callback/server.py` | 回调 webhook（URL 验证 + 消息通知触发拉取） |
| `callback/crypto.py` | 回调消息的 AES 加解密（WXBizMsgCrypt） |
| `core/fetcher.py` | 消息拉取主循环，线程安全，触发保单识别 |
| `core/decryptor.py` | RSA + SDK AES 两阶段消息解密 |
| `core/parser.py` | 消息类型分发解析（text/image/voice/video/file/link 等） |
| `core/contacts.py` | 通讯录解析 + 24h 数据库缓存（内部员工/外部联系人/群聊） |
| `core/cos_storage.py` | 腾讯云 COS 媒体上传（支持文件和字节流） |
| `sdk/finance_sdk.py` | ctypes 封装 libWeWorkFinanceSdk_C.so（支持下载到内存） |
| `storage/db.py` | MySQL 连接池操作（PooledDB + pymysql） |
| `web/api.py` | 消息查询 REST API |
| `web/templates/index.html` | 消息查询前端 |

### 保单识别模块（insurance/）

| 模块 | 职责 |
|------|------|
| `insurance/handler.py` | 队列串行处理器，OCR 流水线，钉钉自动同步 |
| `insurance/api.py` | Flask Blueprint，保单识别 REST API（/api/insurance/*） |
| `insurance/db.py` | 保单识别数据库操作（insurance_records + insurance_config） |
| `insurance/ocr_service.py` | pdfplumber 文本提取（支持 bytes 输入） |
| `insurance/baidu_ocr.py` | 百度 OCR 兜底识别（支持 bytes 输入） |
| `insurance/policy_parser.py` | 保单字段解析（50+ 保险公司规则，1700+ 行） |
| `insurance/field_mapping.py` | OCR 字段 → 导出列映射 |
| `insurance/dingtalk_sync.py` | 钉钉多维表格同步 |
| `insurance/config/` | 字段映射和钉钉目标配置文件 |
| `web/templates/insurance.html` | 保单识别独立前端（/insurance） |

### 数据库

MySQL（库名 pdfocr），使用 PooledDB 连接池，表在首次启动时自动创建。

- `messages` — 消息存储（索引：msgtime, msgtype, sender, roomid）
- `cursor_state` — seq 游标（单行，id=1），支持增量拉取
- `contacts_cache` — 通讯录缓存（24h TTL）
- `insurance_records` — 保单识别记录（索引：roomid, status, created_at）
- `insurance_config` — 保单识别配置（前端可配置）

### 配置

`config.yaml` 包含：
- `wecom` — 企业 ID、存档密钥
- `callback` — 端口、token、EncodingAESKey
- `sdk` — C 库路径、RSA 私钥路径
- `mysql` — MySQL 连接配置（host, port, user, password, database）
- `fetch` — 单次拉取上限（max 1000）
- `cos` — 腾讯云 COS（可选）

保单识别的配置（监控群、百度OCR、钉钉应用）存储在 `insurance_config` 数据库表中，通过前端页面管理。

### 外部依赖

- **企业微信 Finance SDK**：C 动态库 `libWeWorkFinanceSdk_C.so`，需单独部署
- **企业微信 API**：通讯录/群聊名称解析
- **腾讯云 COS**（可选）：媒体文件和保单 PDF 云存储
- **百度 OCR**（可选）：扫描件 PDF 识别兜底
- **钉钉多维表 API**：保单识别结果同步

## 页面路由

- `/` — 消息监控页面
- `/insurance` — 保单识别页面（手动上传 + 自动识别记录 + 设置管理）

## 部署与运行权限（重要）

- **禁止私自部署或运行**：任何部署到正式服务器（43.136.92.192）的操作（上传代码、重启进程、重载 gunicorn、kill/启动服务等），以及在服务器上执行会改变运行状态的命令，必须先征得用户明确同意后才能执行
- 未经用户确认，只允许在服务器上执行只读命令（查看日志、ps、ls 等排查类操作）
- 本地代码修改、git commit/push 不受此限制

## 开发注意事项

- `data/` 目录（媒体、私钥）已 gitignore，不可提交
- SDK C 库仅支持 Linux，macOS 开发时 SDK 相关功能会降级（前端可用）
- 回调触发的消息拉取在独立线程中执行，注意线程安全
- 保单识别使用 Queue 串行消费，同一时刻只处理一个 PDF
- 通讯录解析依赖企业微信 API，有频率限制，已通过数据库缓存缓解
- policy_parser.py 包含 50+ 保险公司的正则规则，新增保司需持续维护

## 新增保单提取字段的完整流程

新增一个 OCR 提取字段（如车架号、身份证号码）需要改动以下 **4 个文件**，缺一不可：

### 1. `insurance/policy_parser.py` — 提取规则（核心）

- 编写正则提取逻辑（函数或在 `_extract_common_fields` 中调用）
- 将提取结果写入 `fields["字段名"]`
- 更新底部的 `FIELD_ORDER` 列表（控制导出时字段排序）

### 2. `insurance/field_mapping.py` — 字段映射

- `OUTPUT_COLUMNS`：新增导出列名（如 `"车架号"`）
- `DEFAULT_MAPPING`：新增 OCR 字段 → 导出列的映射（如 `"车架号VIN": "车架号"`）
- `get_available_ocr_fields()`：新增可选字段名

### 3. `insurance/config/field_mapping.json` — 持久化配置

- `output_columns` 数组：新增导出列名
- `default_mapping` 对象：新增映射关系
- 该文件在首次启动时自动生成，后续新增字段需手动添加或删除此文件让系统重新生成

### 4. `insurance/field_config_db.py` — 前端列配置

- `DEFAULT_COLUMNS`：新增字段定义（key、visible、order、display_name）
- 这是**前端「页面列配置」和「导出列配置」中显示的字段列表**
- 不加到这里 → 前端配置页面看不到该字段，用户无法勾选显示
- `_fix_missing_defaults()` 会自动将新字段补充到已有模板末尾（默认 `visible=False`，不影响已有模板）

### 注意事项

- 4 个文件的字段名必须对应：`policy_parser` 提取的 key → `field_mapping` 映射 → `field_config_db` 前端显示
- 已有用户的模板：新字段自动补充但默认不勾选（`visible=False`），用户手动开启
- 新用户/新模板：按 `DEFAULT_COLUMNS` 中的 `visible` 值决定是否默认显示
