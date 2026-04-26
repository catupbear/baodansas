"""
SQLite → MySQL 数据迁移脚本
将 ./data/wxbot.db 中的历史数据迁移到 MySQL（config.yaml 中配置的库）

迁移的表：
  - messages       逐行 INSERT IGNORE（seq 是 UNIQUE，防重复）
  - cursor_state   REPLACE INTO（只有一行）
  - contacts_cache REPLACE INTO

用法：
  python3 migrate_sqlite_to_mysql.py
"""

import os
import sqlite3
import sys

import pymysql
import yaml

# SQLite 数据库路径
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "data", "wxbot.db")
# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
# 进度打印间隔（每 N 条打印一次）
PROGRESS_INTERVAL = 100


def load_mysql_config() -> dict:
    """从 config.yaml 读取 MySQL 连接配置"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("mysql", {})


def connect_mysql(cfg: dict) -> pymysql.connections.Connection:
    """创建 MySQL 直连（不使用连接池）"""
    return pymysql.connect(
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 3306),
        user=cfg.get("user", "root"),
        password=cfg.get("password", ""),
        database=cfg.get("database", "pdfocr"),
        charset="utf8mb4",
        autocommit=False,
    )


def migrate_messages(sqlite_conn: sqlite3.Connection, mysql_conn: pymysql.connections.Connection) -> dict:
    """
    迁移 messages 表。
    使用 INSERT IGNORE，seq 字段上有 UNIQUE 约束，重复行会被跳过。
    返回统计信息 {"total": N, "inserted": N, "skipped": N}
    """
    print("\n[messages] 开始迁移...")

    sqlite_cursor = sqlite_conn.cursor()
    mysql_cursor = mysql_conn.cursor()

    # 查询 SQLite 中 messages 的列名，做动态适配
    sqlite_cursor.execute("PRAGMA table_info(messages)")
    columns_info = sqlite_cursor.fetchall()
    if not columns_info:
        print("[messages] SQLite 中不存在 messages 表，跳过。")
        return {"total": 0, "inserted": 0, "skipped": 0}

    columns = [row[1] for row in columns_info]  # row[1] 是列名
    print(f"[messages] SQLite 列: {columns}")

    # 查询所有数据（按 seq 升序，方便观察进度）
    sqlite_cursor.execute("SELECT * FROM messages ORDER BY seq ASC")
    rows = sqlite_cursor.fetchall()
    total = len(rows)
    print(f"[messages] 共 {total} 条数据待迁移")

    inserted = 0
    skipped = 0
    failed = 0

    # 构造 INSERT IGNORE SQL（列名与 SQLite 完全对应）
    col_names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT IGNORE INTO messages ({col_names}) VALUES ({placeholders})"

    for i, row in enumerate(rows, start=1):
        try:
            mysql_cursor.execute(sql, row)
            # rowcount == 1 表示插入成功，0 表示因 IGNORE 被跳过
            if mysql_cursor.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            seq_val = row[columns.index("seq")] if "seq" in columns else "?"
            print(f"  [WARN] seq={seq_val} 插入失败: {e}")

        # 每 PROGRESS_INTERVAL 条打印进度并提交
        if i % PROGRESS_INTERVAL == 0:
            mysql_conn.commit()
            print(f"  进度: {i}/{total}  已插入={inserted}  跳过={skipped}  失败={failed}")

    # 提交剩余数据
    mysql_conn.commit()
    print(f"[messages] 完成: 共={total}  已插入={inserted}  跳过={skipped}  失败={failed}")
    return {"total": total, "inserted": inserted, "skipped": skipped, "failed": failed}


def migrate_cursor_state(sqlite_conn: sqlite3.Connection, mysql_conn: pymysql.connections.Connection) -> dict:
    """
    迁移 cursor_state 表（只有 id=1 这一行）。
    使用 REPLACE INTO，覆盖 MySQL 中已有的行。
    """
    print("\n[cursor_state] 开始迁移...")

    sqlite_cursor = sqlite_conn.cursor()
    mysql_cursor = mysql_conn.cursor()

    # 检查表是否存在
    sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cursor_state'")
    if not sqlite_cursor.fetchone():
        print("[cursor_state] SQLite 中不存在 cursor_state 表，跳过。")
        return {"total": 0, "inserted": 0}

    sqlite_cursor.execute("PRAGMA table_info(cursor_state)")
    columns = [row[1] for row in sqlite_cursor.fetchall()]
    print(f"[cursor_state] SQLite 列: {columns}")

    sqlite_cursor.execute("SELECT * FROM cursor_state")
    rows = sqlite_cursor.fetchall()
    total = len(rows)

    if total == 0:
        print("[cursor_state] SQLite 中没有数据，跳过。")
        return {"total": 0, "inserted": 0}

    col_names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"REPLACE INTO cursor_state ({col_names}) VALUES ({placeholders})"

    inserted = 0
    for row in rows:
        try:
            mysql_cursor.execute(sql, row)
            inserted += 1
        except Exception as e:
            print(f"  [WARN] cursor_state 插入失败: {e}")

    mysql_conn.commit()
    print(f"[cursor_state] 完成: 共={total}  已写入={inserted}")
    return {"total": total, "inserted": inserted}


def migrate_contacts_cache(sqlite_conn: sqlite3.Connection, mysql_conn: pymysql.connections.Connection) -> dict:
    """
    迁移 contacts_cache 表。
    使用 REPLACE INTO，以 id（VARCHAR 主键）为准进行覆盖。
    """
    print("\n[contacts_cache] 开始迁移...")

    sqlite_cursor = sqlite_conn.cursor()
    mysql_cursor = mysql_conn.cursor()

    # 检查表是否存在
    sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contacts_cache'")
    if not sqlite_cursor.fetchone():
        print("[contacts_cache] SQLite 中不存在 contacts_cache 表，跳过。")
        return {"total": 0, "inserted": 0}

    sqlite_cursor.execute("PRAGMA table_info(contacts_cache)")
    columns = [row[1] for row in sqlite_cursor.fetchall()]
    print(f"[contacts_cache] SQLite 列: {columns}")

    sqlite_cursor.execute("SELECT * FROM contacts_cache")
    rows = sqlite_cursor.fetchall()
    total = len(rows)
    print(f"[contacts_cache] 共 {total} 条数据待迁移")

    if total == 0:
        print("[contacts_cache] SQLite 中没有数据，跳过。")
        return {"total": 0, "inserted": 0}

    col_names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"REPLACE INTO contacts_cache ({col_names}) VALUES ({placeholders})"

    inserted = 0
    failed = 0

    for i, row in enumerate(rows, start=1):
        try:
            mysql_cursor.execute(sql, row)
            inserted += 1
        except Exception as e:
            failed += 1
            id_val = row[columns.index("id")] if "id" in columns else "?"
            print(f"  [WARN] id={id_val} 插入失败: {e}")

        # 每 PROGRESS_INTERVAL 条提交并打印进度
        if i % PROGRESS_INTERVAL == 0:
            mysql_conn.commit()
            print(f"  进度: {i}/{total}  已写入={inserted}  失败={failed}")

    mysql_conn.commit()
    print(f"[contacts_cache] 完成: 共={total}  已写入={inserted}  失败={failed}")
    return {"total": total, "inserted": inserted, "failed": failed}


def main():
    # ── 检查 SQLite 文件是否存在 ──────────────────────────────────────────────
    if not os.path.exists(SQLITE_PATH):
        print(f"[错误] SQLite 数据库文件不存在: {SQLITE_PATH}")
        print("请确认 ./data/wxbot.db 文件存在后再运行本脚本。")
        sys.exit(1)

    print(f"SQLite 路径  : {SQLITE_PATH}")

    # ── 读取 MySQL 配置 ────────────────────────────────────────────────────────
    if not os.path.exists(CONFIG_PATH):
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)

    mysql_cfg = load_mysql_config()
    print(f"MySQL 目标   : {mysql_cfg.get('user')}@{mysql_cfg.get('host')}:{mysql_cfg.get('port')}/{mysql_cfg.get('database')}")

    # ── 建立连接 ───────────────────────────────────────────────────────────────
    print("\n正在连接数据库...")
    try:
        sqlite_conn = sqlite3.connect(SQLITE_PATH)
        sqlite_conn.row_factory = None  # 使用默认 tuple 模式，保持顺序一致
        print("  SQLite 连接成功")
    except Exception as e:
        print(f"[错误] 无法打开 SQLite 数据库: {e}")
        sys.exit(1)

    try:
        mysql_conn = connect_mysql(mysql_cfg)
        print("  MySQL 连接成功")
    except Exception as e:
        print(f"[错误] 无法连接 MySQL: {e}")
        sqlite_conn.close()
        sys.exit(1)

    # ── 执行迁移 ───────────────────────────────────────────────────────────────
    try:
        stats_messages = migrate_messages(sqlite_conn, mysql_conn)
        stats_cursor = migrate_cursor_state(sqlite_conn, mysql_conn)
        stats_contacts = migrate_contacts_cache(sqlite_conn, mysql_conn)
    finally:
        sqlite_conn.close()
        mysql_conn.close()

    # ── 打印汇总统计 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print("迁移完成，汇总统计：")
    print(f"  messages      : 总计={stats_messages['total']}  "
          f"插入={stats_messages.get('inserted', 0)}  "
          f"跳过={stats_messages.get('skipped', 0)}  "
          f"失败={stats_messages.get('failed', 0)}")
    print(f"  cursor_state  : 总计={stats_cursor['total']}  "
          f"写入={stats_cursor.get('inserted', 0)}")
    print(f"  contacts_cache: 总计={stats_contacts['total']}  "
          f"写入={stats_contacts.get('inserted', 0)}  "
          f"失败={stats_contacts.get('failed', 0)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
