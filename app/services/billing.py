"""通行费费率版本与行程账单。

* **费率版本**：财务按航段提交整组半开分钟费率区间（起点非负、终点大于
  起点、费率为非负整数分/分钟），**整组替换**该段旧费率并**继承**上一
  版本其他航段，形成**全局不可变费率版本**（版本号自增、只增不改）；
  组内重叠或未知航段**整次拒绝**（不产生新版本），未覆盖分钟费率为零。
* **开单**：凭行程号在单个 ``BEGIN IMMEDIATE`` 事务内按**当时最新费率
  版本**与确认快照各段**进入分钟**选定费率、以**固化航时**计费
  （金额 = 费率 × 航时，整数分），逐段金额与总额随账单固化——旧账单
  不随费率改变。每个行程只开一次，重试返回原账单；未知行程或**已作废
  且未开单**的行程拒绝开单。
* **冲销**：作废已开单行程时在**原作废事务**内生成等额冲销单（每账单
  至多一张）；重复作废不重复冲销。账单与冲销单均可查。

并发：开单与作废都在 ``BEGIN IMMEDIATE`` 事务内串行——先提交的开单会
被后到的作废看到并冲销；先提交的作废使后到的开单按 ``TRIP_VOIDED``
拒绝，因此不会遗漏冲销，也不会重复冲销。开单/作废均不写
``confirmed_trips``、``batch_confirmations``，也不影响规划占用。
"""
import json
import sqlite3

from ..errors import ApiError
from .controls import segment_exists


# ---- 费率版本 ----------------------------------------------------------------

def validate_rate_intervals(items) -> list[tuple[int, int, int]]:
    """校验提交的费率组，返回规范化后的升序三元组列表（可为空）。

    非法区间（起点为负 / 终点不大于起点 / 费率为负）->
    422 INVALID_RATE_INTERVAL；组内半开区间交叠 ->
    422 OVERLAPPING_RATE_INTERVAL。任一失败整次拒绝、不产生新版本。
    """
    intervals: list[tuple[int, int, int]] = []
    for idx, it in enumerate(items):
        start = it["start_minute"]
        end = it["end_minute"]
        rate = it["rate"]
        if start < 0 or end <= start or rate < 0:
            raise ApiError(
                422,
                "INVALID_RATE_INTERVAL",
                f"第 {idx} 个费率区间非法：要求 start_minute>=0、"
                "end_minute>start_minute、rate 为非负整数（分/分钟）",
                {"index": idx, "start_minute": start, "end_minute": end,
                 "rate": rate},
            )
        intervals.append((start, end, rate))

    intervals.sort(key=lambda x: x[0])
    for i in range(1, len(intervals)):
        prev_start, prev_end, _ = intervals[i - 1]
        start, end, _ = intervals[i]
        if start < prev_end:
            raise ApiError(
                422,
                "OVERLAPPING_RATE_INTERVAL",
                f"费率区间组内交叠：[{prev_start}, {prev_end}) 与 "
                f"[{start}, {end}) 相互重叠，整次拒绝",
                {"previous": {"start_minute": prev_start, "end_minute": prev_end},
                 "current": {"start_minute": start, "end_minute": end}},
            )
    return intervals


def latest_rate_version(conn) -> int | None:
    """当前最新费率版本号；尚无任何版本时返回 None。"""
    row = conn.execute(
        "SELECT version FROM toll_rate_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row["version"] if row is not None else None


def load_rates(conn, version: int, seg_ids=None) -> dict[int, list[tuple[int, int, int]]]:
    """读取某版本费率表，返回 {seg_id: [(start, end, rate), ...]}（按起点升序）。

    seg_ids 为 None 时读取全部航段。
    """
    if seg_ids is not None:
        seg_ids = list(seg_ids)
        if not seg_ids:
            return {}
        placeholders = ",".join("?" for _ in seg_ids)
        rows = conn.execute(
            f"SELECT seg_id, start_minute, end_minute, rate FROM toll_rates"
            f" WHERE version = ? AND seg_id IN ({placeholders})"
            f" ORDER BY seg_id, start_minute",
            (version, *seg_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT seg_id, start_minute, end_minute, rate FROM toll_rates"
            " WHERE version = ? ORDER BY seg_id, start_minute",
            (version,),
        ).fetchall()
    rates: dict[int, list[tuple[int, int, int]]] = {}
    for r in rows:
        rates.setdefault(r["seg_id"], []).append(
            (r["start_minute"], r["end_minute"], r["rate"])
        )
    return rates


def rate_at(intervals: list[tuple[int, int, int]], minute: int) -> int:
    """进入分钟 minute 命中的费率（分/分钟）；未覆盖分钟费率为 0。"""
    for start, end, rate in intervals:
        if start <= minute < end:
            return rate
        if minute < start:
            break
    return 0


def submit_segment_rates(conn, seg_id: int, items, note=None) -> dict:
    """按航段整组提交费率区间，生成新的全局不可变费率版本。

    整组替换该段旧费率、继承上一版本其他航段；空组清除该段费率。未知航段
    -> 404 SEGMENT_NOT_FOUND；非法/交叠区间整次拒绝（不产生新版本）。
    返回新版本号与该航段在新版本下的生效清单（按起点升序）。
    """
    if not segment_exists(conn, seg_id):
        raise ApiError(404, "SEGMENT_NOT_FOUND",
                       f"未知航段: {seg_id}", {"seg_id": seg_id})

    intervals = validate_rate_intervals(items)

    try:
        conn.execute("BEGIN IMMEDIATE")
        prev = latest_rate_version(conn)
        cur = conn.execute(
            "INSERT INTO toll_rate_versions(seg_id, note) VALUES (?, ?)",
            (seg_id, note),
        )
        version = cur.lastrowid
        if prev is not None:
            # 继承上一版本其他航段的费率（本航段由新组整组替换）
            conn.execute(
                "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute, rate)"
                " SELECT ?, seg_id, start_minute, end_minute, rate FROM toll_rates"
                " WHERE version = ? AND seg_id <> ?",
                (version, prev, seg_id),
            )
        conn.executemany(
            "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute, rate)"
            " VALUES (?, ?, ?, ?, ?)",
            [(version, seg_id, s, e, r) for s, e, r in intervals],
        )
        row = conn.execute(
            "SELECT version, seg_id, note, created_at FROM toll_rate_versions"
            " WHERE version = ?",
            (version,),
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "version": row["version"],
        "seg_id": row["seg_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "rates": [
            {"start_minute": s, "end_minute": e, "rate": r}
            for s, e, r in intervals
        ],
    }


def list_rate_versions(conn) -> list[dict]:
    """全部费率版本（按版本号升序）。"""
    rows = conn.execute(
        "SELECT version, seg_id, note, created_at FROM toll_rate_versions"
        " ORDER BY version"
    ).fetchall()
    return [dict(r) for r in rows]


def rate_version_detail(conn, version: int) -> dict:
    """某版本的全量费率表（继承 + 替换后的快照）。未知版本 404。"""
    row = conn.execute(
        "SELECT version, seg_id, note, created_at FROM toll_rate_versions"
        " WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "RATE_VERSION_NOT_FOUND",
                       f"费率版本不存在: {version}", {"version": version})
    rates = load_rates(conn, version)
    entries = [
        {"seg_id": seg_id, "start_minute": s, "end_minute": e, "rate": r}
        for seg_id, intervals in sorted(rates.items())
        for s, e, r in intervals
    ]
    return {**dict(row), "rates": entries}


def current_segment_rates(conn, seg_id: int) -> dict:
    """航段在最新版本下的生效费率清单；尚无版本时 version 为 null、清单为空
    （未覆盖分钟费率为零）。未知航段 404 SEGMENT_NOT_FOUND。"""
    if not segment_exists(conn, seg_id):
        raise ApiError(404, "SEGMENT_NOT_FOUND",
                       f"未知航段: {seg_id}", {"seg_id": seg_id})
    version = latest_rate_version(conn)
    intervals = (
        load_rates(conn, version).get(seg_id, []) if version is not None else []
    )
    return {
        "seg_id": seg_id,
        "version": version,
        "rates": [
            {"start_minute": s, "end_minute": e, "rate": r}
            for s, e, r in intervals
        ],
    }


# ---- 账单 --------------------------------------------------------------------


def _trip_exists(conn, trip_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
    ).fetchone() is not None


def _bill_record(row) -> dict:
    return {
        "bill_id": row["bill_id"],
        "trip_id": row["trip_id"],
        "rate_version": row["rate_version"],
        "legs": json.loads(row["legs"]),
        "total_amount": row["total_amount"],
        "billed_at": row["billed_at"],
    }


def _reversal_record(row) -> dict:
    return {
        "reversal_id": row["reversal_id"],
        "bill_id": row["bill_id"],
        "trip_id": row["trip_id"],
        "amount": row["amount"],
        "reversed_at": row["reversed_at"],
    }


def issue_bill(conn, trip_id: int) -> tuple[dict, bool]:
    """为已确认行程开具通行费账单。返回 (账单, replayed)。

    单个 ``BEGIN IMMEDIATE`` 事务内：行程存在（否则 404 TRIP_NOT_FOUND）
    -> 已开单则直接返回**原账单**（replayed=True，旧账单不随费率改变）
    -> 已作废且未开单拒绝（409 TRIP_VOIDED）-> 取**当时最新费率版本**
    （尚无版本 404 RATE_VERSION_NOT_FOUND）-> 按确认快照各段进入分钟选
    费率、以固化航时计费，写入不可变账单。并发重复开单由 trip_id 唯一
    约束兜底，回滚后按原账单返回。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        trip = conn.execute(
            "SELECT trip_id, itinerary FROM confirmed_trips WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        if trip is None:
            raise ApiError(404, "TRIP_NOT_FOUND",
                           f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})

        existing = conn.execute(
            "SELECT * FROM toll_bills WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        if existing is not None:
            # 每个行程只开一次：重试返回原账单（不随后续费率/作废改变）
            conn.execute("COMMIT")
            return _bill_record(existing), True

        if conn.execute(
            "SELECT 1 FROM voided_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone() is not None:
            raise ApiError(
                409, "TRIP_VOIDED",
                f"行程 {trip_id} 已作废且未开单，拒绝开单",
                {"trip_id": trip_id},
            )

        version = latest_rate_version(conn)
        if version is None:
            raise ApiError(404, "RATE_VERSION_NOT_FOUND",
                           "尚未发布任何通行费费率版本，无法开单")

        legs = json.loads(trip["itinerary"])
        rates = load_rates(conn, version, {leg["seg_id"] for leg in legs})
        bill_legs = []
        total = 0
        for idx, leg in enumerate(legs):
            # 按该段进入分钟选定费率（途中跨费率边界不变），以固化航时计费
            rate = rate_at(rates.get(leg["seg_id"], []), leg["start_minute"])
            amount = rate * leg["travel_time"]
            total += amount
            bill_legs.append({
                "leg_index": idx,
                "seg_id": leg["seg_id"],
                "origin": leg["origin"],
                "destination": leg["destination"],
                "start_minute": leg["start_minute"],
                "travel_time": leg["travel_time"],
                "rate": rate,
                "amount": amount,
            })

        try:
            cur = conn.execute(
                "INSERT INTO toll_bills(trip_id, rate_version, total_amount, legs)"
                " VALUES (?, ?, ?, ?)",
                (trip_id, version, total, json.dumps(bill_legs)),
            )
        except sqlite3.IntegrityError:
            # 并发竞争：另一事务刚为同一行程开单，回滚后按原账单返回
            conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT * FROM toll_bills WHERE trip_id = ?", (trip_id,)
            ).fetchone()
            return _bill_record(row), True

        row = conn.execute(
            "SELECT * FROM toll_bills WHERE bill_id = ?", (cur.lastrowid,)
        ).fetchone()
        conn.execute("COMMIT")
        return _bill_record(row), False
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_bill(conn, trip_id: int) -> dict:
    """按行程号查询账单。未知行程 404 TRIP_NOT_FOUND；未开单 404 BILL_NOT_FOUND。"""
    if not _trip_exists(conn, trip_id):
        raise ApiError(404, "TRIP_NOT_FOUND",
                       f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})
    row = conn.execute(
        "SELECT * FROM toll_bills WHERE trip_id = ?", (trip_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "BILL_NOT_FOUND",
                       f"行程 {trip_id} 尚未开单，不存在账单",
                       {"trip_id": trip_id})
    return _bill_record(row)


def list_bills(conn) -> list[dict]:
    """全部账单（按账单号升序）。"""
    rows = conn.execute(
        "SELECT * FROM toll_bills ORDER BY bill_id"
    ).fetchall()
    return [_bill_record(r) for r in rows]


# ---- 冲销单 ------------------------------------------------------------------


def create_reversal_if_billed(conn, trip_id: int):
    """在作废事务内调用：行程已开单则生成等额冲销单并返回记录，否则 None。

    每张账单至多一张冲销单（toll_reversals.bill_id 唯一）；首次作废在
    事务内至多发生一次，重复作废不会走到这里，故不会重复冲销。
    """
    bill = conn.execute(
        "SELECT bill_id, total_amount FROM toll_bills WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()
    if bill is None:
        return None
    conn.execute(
        "INSERT INTO toll_reversals(bill_id, trip_id, amount) VALUES (?, ?, ?)",
        (bill["bill_id"], trip_id, bill["total_amount"]),
    )
    return conn.execute(
        "SELECT * FROM toll_reversals WHERE bill_id = ?", (bill["bill_id"],)
    ).fetchone()


def get_reversal(conn, trip_id: int) -> dict:
    """按行程号查询冲销单。

    未知行程 404 TRIP_NOT_FOUND；行程存在但无冲销单 404 REVERSAL_NOT_FOUND。
    """
    if not _trip_exists(conn, trip_id):
        raise ApiError(404, "TRIP_NOT_FOUND",
                       f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})
    row = conn.execute(
        "SELECT * FROM toll_reversals WHERE trip_id = ?", (trip_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "REVERSAL_NOT_FOUND",
                       f"行程 {trip_id} 不存在冲销单",
                       {"trip_id": trip_id})
    return _reversal_record(row)


def list_reversals(conn) -> list[dict]:
    """全部冲销单（按冲销单号升序）。"""
    rows = conn.execute(
        "SELECT * FROM toll_reversals ORDER BY reversal_id"
    ).fetchall()
    return [_reversal_record(r) for r in rows]
