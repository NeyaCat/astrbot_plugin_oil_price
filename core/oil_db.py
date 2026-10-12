"""SQLite 存储层。

设计要点：
- 使用标准库 ``sqlite3``（数据量小，无需额外依赖），同步调用通过
  ``asyncio.to_thread`` 包装，避免阻塞事件循环。
- 写入用 ``ON CONFLICT ... DO UPDATE`` 保证幂等：重复初始化 / 重复抓取
  只会更新，不会产生重复行；且空价格不会覆盖已有有效价格。
- 所有写操作串行化（``asyncio.Lock``），规避 SQLite 写锁竞争。

表结构：
- ``price_daily``       价格快照（历史调价点 + 每日抓取，统一落此表）
- ``adjustment_events`` 调价事件 / 调价日
- ``fetch_log``         抓取日志
- ``meta``              状态键值
- ``province_fetch``    按需补数据的请求闸门（见 :meth:`OilDB.get_province_fetch`）
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS price_daily (
    date       TEXT NOT NULL,
    province   TEXT NOT NULL,
    grade      TEXT NOT NULL,
    price      REAL,
    change     REAL,
    source     TEXT NOT NULL DEFAULT 'showapi',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, province, grade)
);
CREATE INDEX IF NOT EXISTS idx_price_pgd  ON price_daily(province, grade, date);
CREATE INDEX IF NOT EXISTS idx_price_date ON price_daily(date);

CREATE TABLE IF NOT EXISTS adjustment_events (
    adjust_date      TEXT PRIMARY KEY,
    gasoline_change  REAL,
    diesel_change    REAL,
    next_change_date TEXT,
    note             TEXT,
    source           TEXT NOT NULL DEFAULT 'showapi-138-49',
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adj_next ON adjustment_events(next_change_date);

CREATE TABLE IF NOT EXISTS fetch_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    task     TEXT NOT NULL,
    province TEXT,
    status   TEXT NOT NULL,
    rows     INTEGER DEFAULT 0,
    message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON fetch_log(ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 用户偏好：记住每个人常用的省份 / 油号，让「看看油价」「我要加油」可以省参数。
-- 主键刻意取 (platform_id, user_id) 而不是 umo：同一人在不同平台适配器上的
-- 会话 ID 不同，混在一起会串号。
CREATE TABLE IF NOT EXISTS user_prefs (
    platform_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    province    TEXT,
    grade       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (platform_id, user_id)
);

-- 按需补数据的**请求闸门**：记录「某个油价日 × 某个省份」是否已经请求过接口。
--
-- 为什么不能只靠 price_daily 判断：price_daily 只能证明"库里有数"，无法证明
-- "今天已经试过、但上游确实没有这个省"。缺了后者，一个常年拿不到数据的省份会被
-- 每个查询者各触发一次请求（ShowAPI 免费额度是按次算的）。
--
-- day_key 是**油价日**（07:00 分界，见 ``main.GAS_DAY_START_HOUR``），不是自然日。
CREATE TABLE IF NOT EXISTS province_fetch (
    day_key    TEXT NOT NULL,
    province   TEXT NOT NULL,
    status     TEXT NOT NULL,
    source     TEXT,
    rows       INTEGER NOT NULL DEFAULT 0,
    message    TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day_key, province)
);
"""

# 偏好只覆盖本次传入的那一列：``province=None`` 表示"不动这一列"，
# 因此 /油价设置地区 不会顺手把已设好的油号清掉（反之亦然）。
_UPSERT_USER_PREF = """
INSERT INTO user_prefs (platform_id, user_id, province, grade, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(platform_id, user_id) DO UPDATE SET
    province   = COALESCE(excluded.province, user_prefs.province),
    grade      = COALESCE(excluded.grade, user_prefs.grade),
    updated_at = excluded.updated_at;
"""

_USER_PREF_SQL = (
    "SELECT province, grade, updated_at FROM user_prefs "
    "WHERE platform_id = ? AND user_id = ?"
)

# 幂等写入：仅当新值非空时才更新（空串/None 不覆盖已有有效价格）。
_UPSERT_PRICE = """
INSERT INTO price_daily (date, province, grade, price, change, source, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date, province, grade) DO UPDATE SET
    price      = COALESCE(excluded.price, price_daily.price),
    change     = COALESCE(excluded.change, price_daily.change),
    source     = excluded.source,
    updated_at = excluded.updated_at
WHERE excluded.price IS NOT NULL;
"""

_UPSERT_ADJUSTMENT = """
INSERT INTO adjustment_events
    (adjust_date, gasoline_change, diesel_change, next_change_date, note, source, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(adjust_date) DO UPDATE SET
    gasoline_change  = COALESCE(excluded.gasoline_change, adjustment_events.gasoline_change),
    diesel_change    = COALESCE(excluded.diesel_change, adjustment_events.diesel_change),
    next_change_date = COALESCE(excluded.next_change_date, adjustment_events.next_change_date),
    note             = COALESCE(excluded.note, adjustment_events.note),
    source           = excluded.source,
    updated_at       = excluded.updated_at;
"""

# ---------------------------------------------------------------------------
# 按需补数据闸门
# ---------------------------------------------------------------------------
def _has_prices_sql(grades: Sequence[str]) -> str:
    """某省在某日是否已有全部所需标号的价格行。

    ``grades`` 为空时退化为「有没有任意一条有效价」（调用方用于整体判断）。
    """
    clean = [g for g in grades if g]
    if not clean:
        return (
            "SELECT COUNT(*) AS c FROM price_daily "
            "WHERE date = ? AND province = ? AND price IS NOT NULL"
        )
    ph = _placeholders(clean)
    return (
        "SELECT COUNT(DISTINCT grade) AS c FROM price_daily "
        f"WHERE date = ? AND province = ? AND price IS NOT NULL AND grade IN ({ph})"
    )


_UPSERT_PROVINCE_FETCH = """
INSERT INTO province_fetch (day_key, province, status, source, rows, message, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(day_key, province) DO UPDATE SET
    status     = excluded.status,
    source     = excluded.source,
    rows       = excluded.rows,
    message    = excluded.message,
    updated_at = excluded.updated_at;
"""

_PROVINCE_FETCH_SQL = (
    "SELECT day_key, province, status, source, rows, message, updated_at "
    "FROM province_fetch WHERE day_key = ? AND province = ?"
)

_PRUNE_PROVINCE_FETCH = "DELETE FROM province_fetch WHERE day_key < ?"


# 取出每个 (province, grade) 在区间内的最低价及其发生日期。
#
# 外层同样要带 ``date BETWEEN ? AND ?``：只在内层过滤时，若「区间外」存在与
# 「区间内最低价」相等的行，会被 JOIN 一并匹配进来，导致 on_date 指向区间外的
# 日期。ORDER BY 里带上 p.date 才能在同价多日时得到确定结果（取最早一天）。
_MIN_PRICE = """
SELECT p.province AS province,
       p.grade    AS grade,
       p.price    AS min_price,
       p.date     AS on_date
FROM price_daily p
JOIN (
    SELECT province, grade, MIN(price) AS m
    FROM price_daily
    WHERE price IS NOT NULL
      AND date BETWEEN ? AND ?
      AND province IN ({ph_prov})
      AND grade IN ({ph_grade})
    GROUP BY province, grade
) t
  ON p.province = t.province
 AND p.grade = t.grade
 AND p.price = t.m
WHERE p.date BETWEEN ? AND ?
ORDER BY p.province, p.grade, p.date;
"""

# 与 _MIN_PRICE 同源，但额外带回「区间内的调价节点数」，且用窗口函数一次扫描搞定。
#
# 为什么不用 _MIN_PRICE 那种自连接写法：外层查询若不带日期过滤，当「区间外」
# 存在与「区间内最低价」相等的行时会被一起匹配进来，于是 on_date 会指向区间外
# 的日期，且重复行在 ORDER BY 未含日期时取值不确定。窗口函数在**同一份已过滤的
# 行集**上取号，日期必然落在区间内，结果唯一。
#
# ROW_NUMBER 按 (price ASC, date ASC) 排序 ⇒ 取区间内最低价，同价时取最早一天。
# COUNT(*) OVER ⇒ 区间内的节点数（主键 (date, province, grade) 保证同一天同组一行）。
_RANGE_STATS = """
SELECT province, grade, price AS min_price, date AS on_date, cnt AS node_count, max_price
FROM (
    SELECT p.province AS province,
           p.grade    AS grade,
           p.price    AS price,
           p.date     AS date,
           ROW_NUMBER() OVER (
               PARTITION BY p.province, p.grade
               ORDER BY p.price ASC, p.date ASC
           ) AS rn,
           COUNT(*) OVER (PARTITION BY p.province, p.grade) AS cnt,
           -- 区间上沿（年内最高）：只出资额本身，不额外纠结发生在哪天 ——
           -- 展示侧要的是「今年区间 X–Y，我离两端各多远」，不需要最高价的日期。
           -- 注意：外层是**显式列清单**，新增列必须内外层都写，否则会静默丢字段。
           MAX(p.price) OVER (PARTITION BY p.province, p.grade) AS max_price
    FROM price_daily p
    WHERE p.price IS NOT NULL
      AND p.date BETWEEN ? AND ?
      AND p.province IN ({ph_prov})
      AND p.grade IN ({ph_grade})
)
WHERE rn = 1
ORDER BY province, grade;
"""

# 区间**首末**节点（用于「当月累计变动」）。与 _RANGE_STATS 同样走窗口函数，
# 但排序换成日期：rn_asc=1 取区间内最早一天，rn_desc=1 取最晚一天。
# 每组最多返回 2 行；区间内只有 1 个节点时 asc/desc 指向同一行，只回 1 行 ——
# 调用方必须结合 node_count 判断，别把"只有 1 个节点"误读成"区间内没变动"。
_RANGE_SPAN = """
SELECT province, grade, date, price, node_count, rn_asc, rn_desc
FROM (
    SELECT p.province AS province,
           p.grade    AS grade,
           p.date     AS date,
           p.price    AS price,
           ROW_NUMBER() OVER (
               PARTITION BY p.province, p.grade ORDER BY p.date ASC
           ) AS rn_asc,
           ROW_NUMBER() OVER (
               PARTITION BY p.province, p.grade ORDER BY p.date DESC
           ) AS rn_desc,
           COUNT(*) OVER (PARTITION BY p.province, p.grade) AS node_count
    FROM price_daily p
    WHERE p.price IS NOT NULL
      AND p.date BETWEEN ? AND ?
      AND p.province IN ({ph_prov})
      AND p.grade IN ({ph_grade})
)
WHERE rn_asc = 1 OR rn_desc = 1
ORDER BY province, grade, date;
"""

# 「最近一次调价」：最新价 + 它相对**上一次不同价**的幅度与发生日期。
#
# 为什么不用 recent_by_group(limit=2) 相邻两日差分：库里**每天都补一条快照行**，
# 而调价只在调价日发生。于是「今天」与「上次调价日」两行价格相同 → 相邻差分恒为 0
# → 图上显示「持平」，把"最近调价涨了 3 毛"这件事彻底盖掉。这是一条假信号。
#
# 用 LAG 在**完整序列**上找"上一条与之不同价的行"，精确且无窗口上限：
# - seq：给每行补上紧邻前一行（按日期）的价格与日期；
# - chg：只留"与前一行真的不同价"的行（超过 _TREND_EPS 量级），按日期倒序取第一条；
# - newest：每组最新一行；
# - LEFT JOIN 保证「只有 1 个节点」的组也返回一行（change 为 NULL），不比旧实现少覆盖。
#
# older_date/prev_price 取的是**紧邻的上一快照日**（调价前一天的旧价），
# 调用方拼标签时应以 adjust_date 为**右端**（真实调价日）。
_LAST_CHANGE = """
WITH seq AS (
    SELECT province, grade, date, price,
           LAG(price) OVER (PARTITION BY province, grade ORDER BY date) AS older_price,
           LAG(date)  OVER (PARTITION BY province, grade ORDER BY date) AS older_date
    FROM price_daily
    WHERE price IS NOT NULL
      AND province IN ({ph_prov})
      AND grade IN ({ph_grade})
),
chg AS (
    SELECT province, grade, date, price, older_price, older_date,
           ROW_NUMBER() OVER (
               PARTITION BY province, grade ORDER BY date DESC
           ) AS crn
    FROM seq
    WHERE older_price IS NOT NULL AND ABS(price - older_price) > 0.0005
),
newest AS (
    SELECT province, grade, price AS newest_price, date AS newest_date,
           ROW_NUMBER() OVER (
               PARTITION BY province, grade ORDER BY date DESC
           ) AS nr
    FROM seq
)
SELECT n.province AS province,
       n.grade    AS grade,
       n.newest_price AS newest_price,
       n.newest_date  AS newest_date,
       c.date       AS adjust_date,
       c.older_date AS prev_date,
       c.older_price AS prev_price,
       ROUND(c.price - c.older_price, 4) AS change
FROM newest n
LEFT JOIN chg c
       ON c.crn = 1
      AND c.province = n.province
      AND c.grade = n.grade
WHERE n.nr = 1
ORDER BY n.province, n.grade;
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _placeholders(items: Sequence[Any]) -> str:
    return ",".join("?" for _ in items) or "NULL"


class OilDB:
    """插件维度的 SQLite 封装。"""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ #
    # 同步底层实现
    # ------------------------------------------------------------------ #
    def _init_schema_sync(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _write_many_sync(self, sql: str, rows: list[tuple]) -> int:
        if not rows:
            return 0
        cur = self._conn.cursor()
        cur.executemany(sql, rows)
        self._conn.commit()
        return cur.rowcount

    def _write_sync(self, sql: str, params: tuple) -> int:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        self._conn.commit()
        return cur.rowcount

    def _query_sync(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def _query_one_sync(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self._query_sync(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # 异步包装
    # ------------------------------------------------------------------ #
    async def init_schema(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._init_schema_sync)

    async def upsert_prices(self, rows: Iterable[tuple]) -> int:
        """写入价格行。

        rows 每项为 ``(date, province, grade, price, change, source, updated_at)``。
        ``price`` 为 None 的行会被丢弃（不覆盖已有值）。
        """
        clean = [
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6])
            for r in rows
            if r[3] is not None
        ]
        if not clean:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._write_many_sync, _UPSERT_PRICE, clean)

    async def upsert_adjustment(
        self,
        adjust_date: str,
        gasoline_change: float | None = None,
        diesel_change: float | None = None,
        next_change_date: str | None = None,
        note: str | None = None,
        source: str = "showapi-138-49",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._write_sync,
                _UPSERT_ADJUSTMENT,
                (
                    adjust_date,
                    gasoline_change,
                    diesel_change,
                    next_change_date,
                    note,
                    source,
                    _now(),
                ),
            )

    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync, "SELECT v FROM meta WHERE k = ?", (key,)
            )
        return row["v"] if row else default

    async def next_change_date(self) -> str | None:
        """最近一次调价事件登记的「下次调价日期」（主图页脚倒计时用）。

        数据来源：``_handle_adjustment`` 在每日任务/调价查询时经 138-49 写入
        ``adjustment_events.next_change_date`` —— 读库取值，**不打 ShowAPI**。
        """
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT next_change_date FROM adjustment_events "
                "WHERE next_change_date IS NOT NULL AND next_change_date != '' "
                "ORDER BY adjust_date DESC, rowid DESC LIMIT 1",
                (),
            )
        return row["next_change_date"] if row else None

    async def set_meta(self, key: str, value: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._write_sync,
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(value)),
            )

    async def get_user_pref(self, platform_id: str, user_id: str) -> dict | None:
        """读取某个用户的省份 / 油号偏好；没存过返回 ``None``。

        返回的 dict 形如 ``{"province": "四川", "grade": "92#", "updated_at": "..."}``，
        两项都可能为 ``None``（只设过其中一项时）。
        """
        if not platform_id or not user_id:
            return None
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync, _USER_PREF_SQL, (str(platform_id), str(user_id))
            )
        if row is None:
            return None
        return {
            "province": row["province"] or None,
            "grade": row["grade"] or None,
            "updated_at": row["updated_at"] or "",
        }

    async def set_user_pref(
        self,
        platform_id: str,
        user_id: str,
        *,
        province: str | None = None,
        grade: str | None = None,
    ) -> None:
        """写入偏好；``None`` 的那一列保持原值不变（见 :data:`_UPSERT_USER_PREF`）。"""
        if not platform_id or not user_id:
            return
        async with self._lock:
            await asyncio.to_thread(
                self._write_sync,
                _UPSERT_USER_PREF,
                (str(platform_id), str(user_id), province, grade, _now()),
            )

    # ------------------------------------------------------------------ #
    # 按需补数据闸门
    # ------------------------------------------------------------------ #
    async def has_prices_on(
        self,
        province: str,
        grades: Sequence[str],
        day_key: str,
    ) -> bool:
        """某省在 ``day_key`` 这一天是否已具备 ``grades`` 里**全部**标号的价格。

        ``grades`` 为空表示「有没有任意一条有效价」。

        这是按需补数据的第一道闸门：**有当天的数就不再打接口**，因此每日任务覆盖到的省份
        不会在每个用户的查询里重复触发一次请求。
        """
        if not province or not day_key:
            return False
        clean = [g for g in (grades or []) if g]
        sql = _has_prices_sql(clean)
        params: tuple = (day_key, province, *clean)
        async with self._lock:
            row = await asyncio.to_thread(self._query_one_sync, sql, params)
        if row is None:
            return False
        count = int(row["c"])
        return count > 0 if not clean else count >= len(clean)

    async def get_province_fetch(self, day_key: str, province: str) -> dict | None:
        """读取「某油价日 × 某省份」的请求记录；没请求过返回 ``None``。

        ``status`` 取值：
        - ``"ok"``    本次拉到了该省的数据；
        - ``"empty"`` 请求成功但上游没给这个省（或没给需要的标号）；
        - ``"fail"``  请求失败（``message`` 是原因）。

        三种都算「今天已经试过」，调用方一律不再重试 —— 额度是按次计费的。
        """
        if not day_key or not province:
            return None
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync, _PROVINCE_FETCH_SQL, (day_key, province)
            )
        return dict(row) if row else None

    async def set_province_fetch(self, rows: Iterable[tuple]) -> int:
        """批量写入请求记录。

        rows 每项为 ``(day_key, province, status, source, rows, message, updated_at)``。
        主键冲突即覆盖（同一油价日重复补数据以最后一次为准）。
        """
        clean = list(rows)
        if not clean:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._write_many_sync, _UPSERT_PROVINCE_FETCH, clean
            )

    async def prune_province_fetch(self, keep_before: str) -> int:
        """清理早于 ``keep_before``（ISO 日期）的闸门记录，避免表无限增长。"""
        async with self._lock:
            return await asyncio.to_thread(
                self._write_sync, _PRUNE_PROVINCE_FETCH, (keep_before,)
            )

    async def latest(self, province: str, grade: str) -> dict | None:
        """取某省某标号的最新一条有效价格。"""
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT date, province, grade, price, change, source FROM price_daily "
                "WHERE province = ? AND grade = ? AND price IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                (province, grade),
            )
        return dict(row) if row else None

    async def prev_before(self, province: str, grade: str, before_date: str) -> dict | None:
        """取 ``before_date`` 之前（不含）最近一条有效价格，用于涨跌对比。"""
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT date, price FROM price_daily "
                "WHERE province = ? AND grade = ? AND price IS NOT NULL AND date < ? "
                "ORDER BY date DESC LIMIT 1",
                (province, grade, before_date),
            )
        return dict(row) if row else None

    async def min_price(
        self,
        provinces: Sequence[str],
        grades: Sequence[str],
        start: str,
        end: str,
    ) -> list[dict]:
        """区间内各 (省, 标号) 的最低油价及发生日期。

        ``on_date`` 一定落在 [start, end] 内；同价多日时取最早一天。
        需要「区间内数据点数」请用 :meth:`range_stats`。
        """
        if not provinces or not grades:
            return []
        sql = _MIN_PRICE.format(
            ph_prov=_placeholders(provinces), ph_grade=_placeholders(grades)
        )
        # 参数顺序：子查询的 (start, end) → 子查询的 province/grade → 外层的 (start, end)
        params = (start, end, *provinces, *grades, start, end)
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, params)
        return [dict(r) for r in rows]

    async def range_stats(
        self,
        provinces: Sequence[str],
        grades: Sequence[str],
        start: str,
        end: str,
    ) -> list[dict]:
        """区间内各 (省, 标号) 的最低价、发生日期与**调价节点数**。

        返回 ``[{"province","grade","min_price","max_price","on_date","node_count"}]``，
        每个 (省, 标号) **恰好一行**，``on_date`` 必然落在 [start, end] 内。

        ``min_price`` / ``max_price`` 是区间的**下沿与上沿**（同一份已过滤行集上取
        值，不跨界）。两者相等意味着区间内价格没变过。

        与 :meth:`min_price` 的区别在于多带回 ``node_count``（区间内的数据点数），
        供调用方在区间内节点过少（最低价必然等于当日价）时如实标注，
        而不是把它误报成"历史低点"。
        """
        if not provinces or not grades:
            return []
        sql = _RANGE_STATS.format(
            ph_prov=_placeholders(provinces), ph_grade=_placeholders(grades)
        )
        params = (start, end, *provinces, *grades)
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, params)
        return [dict(r) for r in rows]

    async def range_span(
        self,
        provinces: Sequence[str],
        grades: Sequence[str],
        start: str,
        end: str,
    ) -> list[dict]:
        """区间内各 (省, 标号) 的**首末**价格与节点数。

        返回 ``[{"province","grade","first_price","first_date","last_price",
        "last_date","node_count"}]``，每个 (省, 标号) **恰好一行**。

        区间内只有 1 个节点时 ``first_*`` 与 ``last_*`` 相同且
        ``node_count == 1`` —— 此时``last - first`` 恒为 0，**不代表"区间内没调价"**，
        调用方应据 ``node_count`` 退化为"仅 N 个节点"的标注，而不是报一个 0 变动。
        """
        if not provinces or not grades:
            return []
        sql = _RANGE_SPAN.format(
            ph_prov=_placeholders(provinces), ph_grade=_placeholders(grades)
        )
        params = (start, end, *provinces, *grades)
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, params)

        folded: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["province"], row["grade"])
            entry = folded.setdefault(
                key,
                {
                    "province": row["province"],
                    "grade": row["grade"],
                    "first_price": None,
                    "first_date": "",
                    "last_price": None,
                    "last_date": "",
                    "node_count": int(row["node_count"]),
                },
            )
            if row["rn_asc"] == 1:
                entry["first_price"] = row["price"]
                entry["first_date"] = row["date"]
            if row["rn_desc"] == 1:
                entry["last_price"] = row["price"]
                entry["last_date"] = row["date"]
        return list(folded.values())

    async def last_change(
        self,
        provinces: Sequence[str],
        grades: Sequence[str],
    ) -> list[dict]:
        """最近一次调价：最新价 + 相对上一次不同价的幅度与发生日期。

        返回 ``[{"province","grade","newest_price","newest_date","adjust_date",
        "prev_date","prev_price","change"}]``，每个 (省, 标号) **恰好一行**。

        ``change`` 是"较上次调价"的幅度（**不是**相邻两日差分，也不是当日涨跌）——
        库里每天补快照行，相邻两日往往同价，差分会把真实的调价幅度抹成 0。
        只有 1 个节点时 ``adjust_date/change`` 为 ``None``（无更早可比）。
        """
        if not provinces or not grades:
            return []
        sql = _LAST_CHANGE.format(
            ph_prov=_placeholders(provinces), ph_grade=_placeholders(grades)
        )
        params = (*provinces, *grades)
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, params)
        return [dict(r) for r in rows]

    async def latest_per_province(self, grades: Sequence[str]) -> list[dict]:
        """取每个省份在指定标号下的最新有效价格（用于出图）。"""
        if not grades:
            return []
        ph = _placeholders(grades)
        sql = f"""
        SELECT p.province AS province, p.grade AS grade, p.price AS price, p.date AS date
        FROM price_daily p
        JOIN (
            SELECT province, grade, MAX(date) AS d
            FROM price_daily
            WHERE price IS NOT NULL AND grade IN ({ph})
            GROUP BY province, grade
        ) t ON p.province = t.province AND p.grade = t.grade AND p.date = t.d
        ORDER BY p.province, p.grade;
        """
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, tuple(grades))
        return [dict(r) for r in rows]

    async def recent_by_group(
        self,
        grades: Sequence[str],
        provinces: Sequence[str] | None = None,
        limit: int = 2,
    ) -> list[dict]:
        """取每个 (province, grade) 最近 ``limit`` 条有效价格（按日期倒序）。

        用于涨跌对比：第 1 条为最新价，第 2 条为上一条价。
        """
        if not grades:
            return []
        ph_grade = _placeholders(grades)
        where = ["price IS NOT NULL", f"grade IN ({ph_grade})"]
        params: list = list(grades)
        if provinces:
            where.append(f"province IN ({_placeholders(provinces)})")
            params.extend(provinces)
        sql = f"""
        SELECT province, grade, date, price FROM (
            SELECT province, grade, date, price,
                   ROW_NUMBER() OVER (
                       PARTITION BY province, grade ORDER BY date DESC
                   ) AS rn
            FROM price_daily
            WHERE {' AND '.join(where)}
        )
        WHERE rn <= ?
        ORDER BY province, grade, date DESC;
        """
        params.append(limit)
        async with self._lock:
            rows = await asyncio.to_thread(self._query_sync, sql, tuple(params))
        return [dict(r) for r in rows]

    async def latest_adjustment(self) -> dict | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT * FROM adjustment_events ORDER BY adjust_date DESC LIMIT 1",
            )
        return dict(row) if row else None

    async def count_prices(self) -> int:
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT COUNT(*) AS c FROM price_daily WHERE price IS NOT NULL",
            )
        return int(row["c"]) if row else 0

    async def distinct_dates(self) -> int:
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                "SELECT COUNT(DISTINCT date) AS c FROM price_daily WHERE price IS NOT NULL",
            )
        return int(row["c"]) if row else 0

    async def insert_log(
        self,
        task: str,
        status: str,
        rows: int = 0,
        message: str = "",
        province: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._write_sync,
                "INSERT INTO fetch_log (ts, task, province, status, rows, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), task, province, status, rows, message[:1000]),
            )

    async def recent_logs(self, limit: int = 10) -> list[dict]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query_sync,
                "SELECT ts, task, status, rows, message FROM fetch_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)
