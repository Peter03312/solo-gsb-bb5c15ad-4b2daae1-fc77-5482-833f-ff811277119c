"""通行费费率版本与行程账单测试。

规则：
* 财务按航段提交整组半开分钟费率区间（起点非负、终点大于起点、费率为
  非负整数分/分钟），整组替换该段旧费率并继承其他航段，形成全局不可变
  费率版本；组内重叠或未知航段整次拒绝，未覆盖分钟费率为零；
* 开单输入行程号，在同一事务按当时最新费率版本与确认快照各段进入分钟
  选费率、以固化航时计费；输出版本、逐段金额及总额（整数分），旧账单
  不随费率改变；每个行程只开一次，重试返回原账单；未知行程或已作废且
  未开单的行程拒绝开单；
* 作废已开单行程时须在原作废事务内生成等额冲销单；重复作废不重复冲销，
  账单与冲销单均可查；开单/作废并发不可遗漏或重复冲销，且不改已确认
  快照、批次幂等结果或规划占用；原作废接口的请求、响应和状态码保持不变。
"""
import json
import threading

from app.database import connect
from app.errors import ApiError
from app.main import bill_trip, void_confirmed_trip
from app.schemas import TripVoidRequest

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, plan):
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    return r.json()


def _plan_body(rev, **over):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    }
    body.update(over)
    return body


def _ab(client, travel_time=2):
    """A->B 单航段 + 0..39 分钟潮位，返回 (seg_id, rev)。"""
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=travel_time, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    return seg, rev


def _submit_rates(client, seg_id, rates, note=None):
    body = {"rates": rates}
    if note is not None:
        body["note"] = note
    return client.put(f"/api/segments/{seg_id}/toll-rates", json=body)


def _rate(s, e, r):
    return {"start_minute": s, "end_minute": e, "rate": r}


def _bill(client, tid):
    return client.post(f"/api/trips/{tid}/bill")


def _confirmed_trip(client, rev, **over):
    return _confirm(client, _plan(client, _plan_body(rev, **over)))["trip_id"]


# ---------------------------------------------------------------------------
# 1. 费率版本：提交、继承、不可变、查询
# ---------------------------------------------------------------------------

def test_submit_rates_creates_global_version_and_inherits_other_segments(client):
    s1, _ = _ab(client)
    add_node(client, "C")
    s2 = add_segment(client, "B", "C", travel_time=3, depth=99.0)

    r = _submit_rates(client, s1, [_rate(0, 100, 5), _rate(100, 200, 7)],
                      note="航段1费率")
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1
    assert v1["seg_id"] == s1
    assert v1["note"] == "航段1费率"
    assert v1["rates"] == [_rate(0, 100, 5), _rate(100, 200, 7)]

    # 第二次提交另一航段：生成新版本，继承上一版本其他航段
    r = _submit_rates(client, s2, [_rate(50, 150, 9)])
    assert r.status_code == 201
    assert r.json()["version"] == 2
    assert r.json()["rates"] == [_rate(50, 150, 9)]

    # 版本 2 全量费率表 = 继承的航段1 + 新提交的航段2
    detail = client.get("/api/toll/rate-versions/2").json()
    assert detail["version"] == 2 and detail["seg_id"] == s2
    assert detail["rates"] == [
        {"seg_id": s1, **_rate(0, 100, 5)},
        {"seg_id": s1, **_rate(100, 200, 7)},
        {"seg_id": s2, **_rate(50, 150, 9)},
    ]

    # 旧版本不可变：版本 1 仍只有航段1的原始费率
    old = client.get("/api/toll/rate-versions/1").json()
    assert old["rates"] == [
        {"seg_id": s1, **_rate(0, 100, 5)},
        {"seg_id": s1, **_rate(100, 200, 7)},
    ]

    # 版本列表
    versions = client.get("/api/toll/rate-versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["seg_id"] for v in versions] == [s1, s2]

    # 航段当前生效清单（最新版本）
    cur1 = client.get(f"/api/segments/{s1}/toll-rates").json()
    assert cur1["version"] == 2
    assert cur1["rates"] == [_rate(0, 100, 5), _rate(100, 200, 7)]
    cur2 = client.get(f"/api/segments/{s2}/toll-rates").json()
    assert cur2["rates"] == [_rate(50, 150, 9)]


def test_submit_rates_replaces_segment_group_and_empty_group_clears(client):
    s1, _ = _ab(client)
    assert _submit_rates(client, s1, [_rate(0, 100, 5)]).status_code == 201
    # 整组替换该段旧费率
    r = _submit_rates(client, s1, [_rate(0, 50, 6), _rate(50, 90, 8)])
    assert r.status_code == 201
    assert r.json()["version"] == 2
    cur = client.get(f"/api/segments/{s1}/toll-rates").json()
    assert cur["rates"] == [_rate(0, 50, 6), _rate(50, 90, 8)]
    # 空组清除该段费率（未覆盖分钟费率为零）
    r = _submit_rates(client, s1, [])
    assert r.status_code == 201
    assert r.json()["rates"] == []
    cur = client.get(f"/api/segments/{s1}/toll-rates").json()
    assert cur["version"] == 3 and cur["rates"] == []


def test_submit_rates_unknown_segment_rejected(client):
    _ab(client)
    r = _submit_rates(client, 999, [_rate(0, 100, 5)])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"
    # 整次拒绝：不产生新版本
    assert client.get("/api/toll/rate-versions").json() == []
    # 查询未知航段当前费率同样 404
    g = client.get("/api/segments/999/toll-rates")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "SEGMENT_NOT_FOUND"


def test_submit_rates_invalid_interval_rejected(client):
    s1, _ = _ab(client)
    for bad in [
        [_rate(-1, 100, 5)],          # 起点为负
        [_rate(100, 100, 5)],         # 终点等于起点
        [_rate(100, 50, 5)],          # 终点小于起点
        [_rate(0, 100, -1)],          # 费率为负
        [_rate(0, 100, 5), _rate(200, 150, 3)],  # 组内任一非法即整次拒绝
    ]:
        r = _submit_rates(client, s1, bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "INVALID_RATE_INTERVAL"
    # 非整数费率（schema 层拒绝）
    r = _submit_rates(client, s1, [_rate(0, 100, 1.5)])
    assert r.status_code == 422
    # 整次拒绝：不产生任何版本
    assert client.get("/api/toll/rate-versions").json() == []


def test_submit_rates_overlapping_intervals_rejected(client):
    s1, _ = _ab(client)
    r = _submit_rates(client, s1, [_rate(0, 100, 5), _rate(50, 150, 3)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_RATE_INTERVAL"
    # 乱序提交同样判交叠
    r = _submit_rates(client, s1, [_rate(50, 150, 3), _rate(0, 100, 5)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_RATE_INTERVAL"
    # 首尾相接合法（半开区间）
    r = _submit_rates(client, s1, [_rate(0, 100, 5), _rate(100, 150, 3)])
    assert r.status_code == 201
    # 失败不产生新版本：本次为首个版本
    assert [v["version"] for v in client.get("/api/toll/rate-versions").json()] == [1]


def test_rate_version_not_found(client):
    _ab(client)
    r = client.get("/api/toll/rate-versions/7")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RATE_VERSION_NOT_FOUND"
    # 尚无版本时航段当前费率为空清单、version 为 null
    cur = client.get("/api/segments/1/toll-rates").json()
    assert cur == {"seg_id": 1, "version": None, "rates": []}


# ---------------------------------------------------------------------------
# 2. 开单：计费口径、幂等、错误
# ---------------------------------------------------------------------------

def test_bill_computes_per_leg_amounts_by_entry_minute(client):
    s1, rev = _ab(client, travel_time=10)
    # [0,5) 3 分/分钟，[5,100) 7 分/分钟
    assert _submit_rates(client, s1, [_rate(0, 5, 3), _rate(5, 100, 7)]).status_code == 201
    tid = _confirmed_trip(client, rev, start_minute=4, window_minutes=20)

    r = _bill(client, tid)
    assert r.status_code == 201, r.text
    bill = r.json()
    assert bill["replayed"] is False
    assert bill["trip_id"] == tid
    assert bill["rate_version"] == 1
    # 进入分钟 4 命中 [0,5)：费率 3，即使航程跨入 [5,100) 区间也不变
    assert bill["legs"] == [{
        "leg_index": 0, "seg_id": s1, "origin": "A", "destination": "B",
        "start_minute": 4, "travel_time": 10, "rate": 3, "amount": 30,
    }]
    assert bill["total_amount"] == 30
    assert bill["billed_at"]

    # 同一行程只开一次：重试返回原账单（200 + replayed）
    again = _bill(client, tid)
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["bill_id"] == bill["bill_id"]
    assert again.json()["total_amount"] == 30

    # 查询账单（无 replayed 字段）
    g = client.get(f"/api/trips/{tid}/bill")
    assert g.status_code == 200
    assert g.json()["total_amount"] == 30
    assert "replayed" not in g.json()


def test_bill_uncovered_minutes_are_zero(client):
    s1, rev = _ab(client, travel_time=2)
    # 只覆盖 [10,20)，进入分钟 0 未覆盖 -> 费率 0
    assert _submit_rates(client, s1, [_rate(10, 20, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    bill = _bill(client, tid).json()
    assert bill["legs"][0]["rate"] == 0
    assert bill["legs"][0]["amount"] == 0
    assert bill["total_amount"] == 0


def test_bill_multi_leg_uses_per_segment_entry_minute(client):
    add_node(client, "A")
    add_node(client, "B")
    add_node(client, "C")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=3, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    assert _submit_rates(client, s1, [_rate(0, 40, 4)]).status_code == 201
    assert _submit_rates(client, s2, [_rate(0, 2, 10), _rate(2, 40, 20)]).status_code == 201

    tid = _confirmed_trip(client, rev, origin="A", destination="C",
                          window_minutes=20)
    bill = _bill(client, tid).json()
    assert bill["rate_version"] == 2
    legs = bill["legs"]
    # 第一段：s1@0 费率 4 × 航时 2 = 8；第二段：s2@2 命中 [2,40) 费率 20 × 3 = 60
    assert [(l["seg_id"], l["start_minute"], l["rate"], l["amount"]) for l in legs] == [
        (s1, 0, 4, 8), (s2, 2, 20, 60),
    ]
    assert bill["total_amount"] == 68


def test_bill_uses_latest_version_and_old_bills_never_change(client):
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    t1 = _confirmed_trip(client, rev)
    b1 = _bill(client, t1).json()
    assert b1["rate_version"] == 1 and b1["total_amount"] == 10

    # 费率变更后：新行程按新版本开单，旧账单不随费率改变
    assert _submit_rates(client, s1, [_rate(0, 40, 9)]).status_code == 201
    # 先作废 t1 释放占用，再确认同分钟新行程
    client.post(f"/api/trips/{t1}/void", json={"reason": "误确认"})
    t2 = _confirmed_trip(client, rev)
    b2 = _bill(client, t2).json()
    assert b2["rate_version"] == 2 and b2["total_amount"] == 18

    g1 = client.get(f"/api/trips/{t1}/bill").json()
    assert g1["rate_version"] == 1 and g1["total_amount"] == 10
    # 重试已开单行程仍返回原账单（即使费率已更新、行程已作废）
    again = _bill(client, t1)
    assert again.status_code == 200
    assert again.json()["rate_version"] == 1
    assert again.json()["total_amount"] == 10


def test_bill_unknown_trip_rejected(client):
    _ab(client)
    r = _bill(client, 999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"
    g = client.get("/api/trips/999/bill")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_bill_without_any_rate_version_rejected(client):
    _, rev = _ab(client)
    tid = _confirmed_trip(client, rev)
    r = _bill(client, tid)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RATE_VERSION_NOT_FOUND"
    # 未开单
    assert client.get(f"/api/trips/{tid}/bill").status_code == 404


def test_bill_voided_unbilled_trip_rejected(client):
    s1, rev = _ab(client)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    assert client.post(f"/api/trips/{tid}/void",
                       json={"reason": "误确认"}).status_code == 201
    r = _bill(client, tid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_VOIDED"
    # 仍无账单、无冲销单
    assert client.get(f"/api/trips/{tid}/bill").status_code == 404
    assert client.get(f"/api/trips/{tid}/reversal").status_code == 404


def test_get_bill_not_found(client):
    _, rev = _ab(client)
    tid = _confirmed_trip(client, rev)
    g = client.get(f"/api/trips/{tid}/bill")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "BILL_NOT_FOUND"


def test_bill_zero_leg_trip_total_zero(client):
    _, rev = _ab(client)
    assert _submit_rates(client, 1, [_rate(0, 40, 5)]).status_code == 201
    # 起点即终点：零航段行程
    plan = _plan(client, _plan_body(rev, origin="A", destination="A"))
    assert plan["itinerary"] == []
    tid = _confirm(client, plan)["trip_id"]
    bill = _bill(client, tid).json()
    assert bill["legs"] == [] and bill["total_amount"] == 0


def test_bill_does_not_change_snapshot_or_occupancy(client):
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    plan = _plan(client, _plan_body(rev))
    tid = _confirm(client, plan)["trip_id"]
    _bill(client, tid)

    # 确认快照不变
    snap = client.get(f"/api/trips/{tid}").json()
    assert snap["arrival_minute"] == 2
    assert [l["start_minute"] for l in snap["itinerary"]] == [0]
    assert snap["voided_at"] is None
    # 规划占用不变：同航段下一艘仍须避开 0,1
    p2 = _plan(client, _plan_body(rev))
    assert [l["start_minute"] for l in p2["itinerary"]] == [2]


def test_bill_batch_confirmed_trip_and_batch_replay_unaffected(client):
    """批次确认的行程同样可开单；批次幂等结果不受开单/冲销影响。"""
    add_node(client, "A")
    add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201

    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": ships}).json()
    body = {
        "revision": rev, "idempotency_key": "batch-bill-demo",
        "ships": [
            {**ships[i], "itinerary": plan["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    ids = [t["trip_id"] for t in r.json()["trips"]]

    # 批次行程开单：走 s1 的船 0 计费 5×2=10，走 s2 的船 1 未覆盖费率为 0
    b0 = _bill(client, ids[0]).json()
    assert b0["total_amount"] == 10
    b1 = _bill(client, ids[1]).json()
    assert b1["total_amount"] == 0

    # 作废船 0 -> 等额冲销；批次原幂等键重放仍返回首次结果
    client.post(f"/api/trips/{ids[0]}/void", json={"reason": "误确认"})
    assert client.get(f"/api/trips/{ids[0]}/reversal").json()["amount"] == 10
    replay = client.post("/api/trips/batch/confirm", json=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert [t["trip_id"] for t in replay.json()["trips"]] == ids


# ---------------------------------------------------------------------------
# 3. 作废冲销：同事务等额冲销、不重复、均可查
# ---------------------------------------------------------------------------

def test_void_billed_trip_creates_equal_reversal_in_same_transaction(client):
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    bill = _bill(client, tid).json()

    r = client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    assert r.status_code == 201

    g = client.get(f"/api/trips/{tid}/reversal")
    assert g.status_code == 200
    reversal = g.json()
    assert reversal["bill_id"] == bill["bill_id"]
    assert reversal["trip_id"] == tid
    assert reversal["amount"] == bill["total_amount"]   # 等额冲销
    assert reversal["reversed_at"]
    # 账单仍可查，内容不变
    b = client.get(f"/api/trips/{tid}/bill").json()
    assert b["total_amount"] == bill["total_amount"]
    # 列表可查
    assert [x["bill_id"] for x in client.get("/api/bills").json()] == [bill["bill_id"]]
    assert [x["reversal_id"] for x in client.get("/api/reversals").json()] == [
        reversal["reversal_id"]]


def test_repeated_void_does_not_duplicate_reversal(client):
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    _bill(client, tid)

    assert client.post(f"/api/trips/{tid}/void",
                       json={"reason": "误确认"}).status_code == 201
    r2 = client.post(f"/api/trips/{tid}/void", json={"reason": "再次作废"})
    assert r2.status_code == 200 and r2.json()["replayed"] is True

    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM toll_reversals WHERE trip_id = ?",
                     (tid,)).fetchone()[0]
    conn.close()
    assert n == 1
    assert len(client.get("/api/reversals").json()) == 1


def test_void_unbilled_trip_creates_no_reversal(client):
    _, rev = _ab(client)
    tid = _confirmed_trip(client, rev)
    assert client.post(f"/api/trips/{tid}/void",
                       json={"reason": "误确认"}).status_code == 201
    g = client.get(f"/api/trips/{tid}/reversal")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "REVERSAL_NOT_FOUND"
    assert client.get("/api/reversals").json() == []


def test_get_reversal_unknown_trip(client):
    _ab(client)
    g = client.get("/api/trips/999/reversal")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_void_interface_unchanged(client):
    """原作废接口的请求、响应和状态码保持不变（响应字段不多不少）。"""
    s1, rev = _ab(client)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    _bill(client, tid)

    r = client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    assert r.status_code == 201
    assert set(r.json().keys()) == {"trip_id", "reason", "voided_at", "replayed"}
    assert r.json()["replayed"] is False

    r2 = client.post(f"/api/trips/{tid}/void", json={"reason": "x"})
    assert r2.status_code == 200
    assert set(r2.json().keys()) == {"trip_id", "reason", "voided_at", "replayed"}
    assert r2.json()["replayed"] is True

    g = client.get(f"/api/trips/{tid}/void")
    assert g.status_code == 200
    assert set(g.json().keys()) == {"trip_id", "reason", "voided_at"}


def test_void_billed_trip_frees_occupancy_and_keeps_bill(client):
    """作废的既有语义不变：占用释放、快照保留；账单与冲销单同时可查。"""
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 40, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    bill = _bill(client, tid).json()

    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    # 占用释放：第 0 分钟重新可规划
    assert [l["start_minute"] for l in _plan(client, _plan_body(rev))["itinerary"]] == [0]
    # 历史快照带 voided_at，账单与冲销单均可查
    assert client.get(f"/api/trips/{tid}").json()["voided_at"] is not None
    assert client.get(f"/api/trips/{tid}/bill").json()["total_amount"] == \
        bill["total_amount"]
    assert client.get(f"/api/trips/{tid}/reversal").json()["amount"] == \
        bill["total_amount"]


# ---------------------------------------------------------------------------
# 4. 并发：开单/作废串行，不遗漏、不重复冲销；重复开单只出一张账单
# ---------------------------------------------------------------------------

def test_concurrent_bill_and_void_never_miss_or_duplicate_reversal(client):
    n = 8
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 1000, 5)]).status_code == 201

    for i in range(n):
        # 每条行程占不同分钟，互不干扰
        tid = _confirmed_trip(client, rev, start_minute=2 * i,
                              window_minutes=20)
        bar = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def do_bill():
            bar.wait()
            try:
                resp = bill_trip(tid)
                with lock:
                    outcomes.append(("bill", resp.status_code,
                                     json.loads(resp.body)))
            except ApiError as exc:
                # 作废先提交：开单按 TRIP_VOIDED 拒绝（HTTP 层映射为 409）
                with lock:
                    outcomes.append(("bill", exc.status_code,
                                     {"error": {"code": exc.code}}))

        def do_void():
            bar.wait()
            resp = void_confirmed_trip(tid, TripVoidRequest(reason="并发作废"))
            with lock:
                outcomes.append(("void", resp.status_code,
                                 json.loads(resp.body)))

        t1 = threading.Thread(target=do_bill)
        t2 = threading.Thread(target=do_void)
        t1.start(); t2.start(); t1.join(); t2.join()

        # 作废一定首次成功
        void_out = [o for o in outcomes if o[0] == "void"][0]
        assert void_out[1] == 201 and void_out[2]["replayed"] is False

        bill_out = [o for o in outcomes if o[0] == "bill"][0]
        conn = connect()
        bills = conn.execute(
            "SELECT * FROM toll_bills WHERE trip_id = ?", (tid,)).fetchall()
        reversals = conn.execute(
            "SELECT * FROM toll_reversals WHERE trip_id = ?", (tid,)).fetchall()
        voided = conn.execute(
            "SELECT COUNT(*) FROM voided_trips WHERE trip_id = ?",
            (tid,)).fetchone()[0]
        conn.close()
        assert voided == 1
        if bill_out[1] == 201:
            # 开单先于作废提交：作废事务必须看到账单并等额冲销（不遗漏）
            assert len(bills) == 1
            assert len(reversals) == 1
            assert reversals[0]["amount"] == bills[0]["total_amount"]
            assert reversals[0]["bill_id"] == bills[0]["bill_id"]
        else:
            # 作废先于开单提交：开单按 TRIP_VOIDED 拒绝，无账单无冲销
            assert bill_out[1] == 409
            assert bill_out[2]["error"]["code"] == "TRIP_VOIDED"
            assert len(bills) == 0
            assert len(reversals) == 0


def test_concurrent_double_bill_single_record(client):
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 100, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)

    bar = threading.Barrier(2)
    results = []

    def b():
        bar.wait()
        resp = bill_trip(tid)
        results.append((resp.status_code, json.loads(resp.body)))

    t1 = threading.Thread(target=b)
    t2 = threading.Thread(target=b)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(r[0] for r in results)
    assert codes == [200, 201]
    bodies = [r[1] for r in results]
    assert bodies[0]["bill_id"] == bodies[1]["bill_id"]
    assert bodies[0]["total_amount"] == bodies[1]["total_amount"]

    conn = connect()
    n_bills = conn.execute(
        "SELECT COUNT(*) FROM toll_bills WHERE trip_id = ?", (tid,)
    ).fetchone()[0]
    conn.close()
    assert n_bills == 1


def test_concurrent_double_void_single_reversal(client):
    """并发作废已开单行程：恰好一条作废记录与一张冲销单。"""
    s1, rev = _ab(client, travel_time=2)
    assert _submit_rates(client, s1, [_rate(0, 100, 5)]).status_code == 201
    tid = _confirmed_trip(client, rev)
    bill = _bill(client, tid).json()

    bar = threading.Barrier(2)
    results = []

    def v(reason):
        bar.wait()
        resp = void_confirmed_trip(tid, TripVoidRequest(reason=reason))
        results.append((resp.status_code, json.loads(resp.body)))

    t1 = threading.Thread(target=v, args=("原因一",))
    t2 = threading.Thread(target=v, args=("原因二",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(r[0] for r in results) == [200, 201]
    conn = connect()
    n_void = conn.execute("SELECT COUNT(*) FROM voided_trips WHERE trip_id = ?",
                          (tid,)).fetchone()[0]
    reversals = conn.execute(
        "SELECT * FROM toll_reversals WHERE trip_id = ?", (tid,)).fetchall()
    conn.close()
    assert n_void == 1
    assert len(reversals) == 1
    assert reversals[0]["amount"] == bill["total_amount"]
