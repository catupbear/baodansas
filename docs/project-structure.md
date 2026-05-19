# 项目目录结构

```
wxbot/
├── main.py                          # 应用入口，加载配置、初始化 Flask 及所有组件
├── config.yaml                      # 运行时配置（企业微信、MySQL、COS 等）
├── config.yaml.example              # 配置文件模板
├── requirements.txt                 # Python 依赖清单
├── run_ocr.py                       # OCR 独立运行脚本
├── migrate_sqlite_to_mysql.py       # SQLite → MySQL 数据迁移工具
├── CLAUDE.md                        # Claude Code 项目指引
├── .gitignore
│
├── auth/                            # 用户认证与权限模块
│   ├── __init__.py
│   ├── api.py                       # 登录/注册/用户管理 API
│   ├── db.py                        # 用户与企业数据库操作
│   ├── decorators.py                # 登录鉴权装饰器
│   └── jwt_utils.py                 # JWT 令牌生成与验证
│
├── callback/                        # 企业微信回调处理
│   ├── __init__.py
│   ├── crypto.py                    # 回调消息 AES 加解密（WXBizMsgCrypt）
│   └── server.py                    # 回调 webhook（URL 验证 + 消息通知触发拉取）
│
├── core/                            # 核心消息处理
│   ├── __init__.py
│   ├── contacts.py                  # 通讯录解析 + 24h 数据库缓存（员工/外部联系人/群聊）
│   ├── cos_storage.py               # 腾讯云 COS 媒体上传（文件和字节流）
│   ├── decryptor.py                 # RSA + SDK AES 两阶段消息解密
│   ├── fetcher.py                   # 消息拉取主循环，线程安全，触发保单识别
│   └── parser.py                    # 消息类型分发解析（25+ 消息类型）
│
├── insurance/                       # 保单识别模块
│   ├── __init__.py
│   ├── api.py                       # 保单识别 REST API（/api/insurance/*）
│   ├── baidu_ocr.py                 # 百度 OCR 兜底识别（支持 bytes 输入）
│   ├── db.py                        # 保单数据库操作（insurance_records + insurance_config）
│   ├── dingtalk_sync.py             # 钉钉多维表格同步
│   ├── field_config_db.py           # 字段配置数据库操作
│   ├── field_mapping.py             # OCR 字段 → 导出列映射
│   ├── handler.py                   # 队列串行处理器，OCR 流水线，钉钉自动同步
│   ├── monitor_config_db.py         # 监控配置数据库操作
│   ├── ocr_service.py               # pdfplumber 文本提取（支持 bytes 输入）
│   ├── policy_parser.py             # 保单字段解析（50+ 保险公司规则，1700+ 行）
│   ├── volc_ocr.py                  # 火山引擎 OCR 服务
│   └── config/                      # 保单识别配置文件
│       ├── dingtalk_targets.json    # 钉钉同步目标配置
│       └── field_mapping.json       # 字段映射配置
│
├── quote/                           # 报价单模块
│   ├── __init__.py
│   ├── binding.py                   # 报价单绑定逻辑
│   ├── handler.py                   # 报价单处理器
│   ├── matcher.py                   # 报价单匹配逻辑
│   └── ocr.py                       # 报价单 OCR 识别
│
├── sdk/                             # 企业微信 Finance SDK 封装
│   ├── __init__.py
│   └── finance_sdk.py               # ctypes 封装 libWeWorkFinanceSdk_C.so（支持下载到内存）
│
├── storage/                         # 数据库存储层
│   ├── __init__.py
│   └── db.py                        # MySQL 连接池操作（PooledDB + pymysql）
│
├── web/                             # Web 层（API + 前端页面）
│   ├── __init__.py
│   ├── api.py                       # 消息查询 REST API
│   └── templates/                   # 前端页面模板
│       ├── index.html               # 消息监控页面（/）
│       ├── insurance.html           # 保单识别页面（/insurance）
│       ├── field_config.html        # 字段配置页面
│       ├── monitor_config.html      # 监控配置页面
│       ├── monitor_login.html       # 监控登录页面
│       ├── training.html            # 训练页面
│       ├── login.html               # 用户登录页面
│       ├── admin_users.html         # 用户管理页面
│       └── admin_enterprises.html   # 企业管理页面
│
├── docs/                            # 项目文档
│   ├── project-structure.md         # 本文件 — 目录结构说明
│   ├── changelog/                   # 更新记录（按月归档）
│   │   ├── 2026-04/                 # 2026年4月更新记录
│   │   └── 2026-05/                 # 2026年5月更新记录
│   ├── design/                      # 技术方案与设计文档
│   ├── product/                     # 产品文档（PRD、功能说明、使用手册）
│   ├── review/                      # 未确认的方案
│   ├── del/                         # 废弃的方案与代码备份
│   └── superpowers/plans/           # 实施计划文档
│
└── data/                            # 运行时数据（已 gitignore）
    ├── media/                       # 下载的媒体文件
    └── *.pem                        # RSA 私钥文件
```
