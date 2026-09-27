"""API 集成测试：每个测试函数使用独立的临时数据库。"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.config
    import app.database
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.database)
    importlib.reload(app.main)

    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as c:
        yield c


def _create_case(client, name="丝绸展柜", location="一号厅"):
    r = client.post("/api/v1/showcases", json={"name": name, "location": location})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_and_root(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert client.get("/").json()["docs"] == "/docs"


def test_showcase_and_exhibit_crud(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "宋代丝帛", "sensitivity": "high"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["annual_limit_lux_hours"] == 15000  # high 默认年度上限
    exhibit_id = body["id"]

    assert client.get(f"/api/v1/exhibits/{exhibit_id}").status_code == 200
    # 未知展品 404
    assert client.get("/api/v1/exhibits/9999").status_code == 404
    # 未知展柜上挂展品 404
    r = client.post(
        "/api/v1/showcases/9999/exhibits",
        json={"name": "x", "sensitivity": "low"},
    )
    assert r.status_code == 404
    # 重名展柜 409
    assert client.post("/api/v1/showcases", json={"name": "丝绸展柜"}).status_code == 409


def test_custom_annual_limit_and_low_sensitivity_unlimited(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "定制", "sensitivity": "medium", "annual_limit_lux_hours": 1000},
    )
    assert r.json()["annual_limit_lux_hours"] == 1000

    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石碑", "sensitivity": "low"},
    )
    assert r.json()["annual_limit_lux_hours"] is None


def test_reject_negative_lux(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -5}
        ]},
    )
    assert r.status_code == 422
    assert "照度" in r.text or "greater than or equal" in r.text


def test_reject_reverse_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422
    assert "结束时间" in r.text


def test_reject_zero_length_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422


def test_unknown_showcase_interval_rejected(client):
    r = client.post(
        "/api/v1/showcases/77/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 404


def test_overlap_budget_max_lux_and_first_exceed(client):
    """端到端：重叠灯光取 max；高敏展品上限 1000 lux·h。

    区间 A: 08:00-18:00 50 lux（背景灯）
    区间 B: 10:00-12:00 200 lux（重点照明，与 A 重叠 → 取 200）
    合并后曝光 = 50*2 + 200*2 + 50*6 = 800 lux·h（当天未超标）
    再加 次日 08:00-16:00 50 lux = 400 → 年度累计 1200，超标 200。
    首次超标：首日 800 之后，次日还需 200，50 lux → 4h，即次日 12:00。
    """
    case_id = _create_case(client, name="预算展柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "丝画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    exhibit_id = r.json()["id"]

    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 200},
            {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 50},
        ]},
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 3

    # 按展柜 + 日期查询（3月1日当天：800，未超标）
    r = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01")
    assert r.status_code == 200, r.text
    budget = r.json()["exhibits"][0]
    assert budget["total_lux_hours"] == 1200.0
    assert budget["daily_used_lux_hours"] == 800.0
    assert budget["first_exceed_at"] is None or budget["first_exceed_at"].startswith("2026-03-02")

    # 按展品查询 3月2日：应报告首次超标时刻与超出量
    r = client.get(f"/api/v1/exhibits/{exhibit_id}/budget?day=2026-03-02")
    budget = r.json()
    assert budget["first_exceed_at"].startswith("2026-03-02T12:00:00")
    assert budget["exceeded_lux_hours"] == 200.0
    assert budget["remaining_lux_hours"] == 0.0
    assert budget["daily_used_lux_hours"] == 400.0
    # 日额度 = 1000 / 365，3月2日剩余 = 日额度 - 400
    assert budget["daily_quota_lux_hours"] == round(1000 / 365, 6)
    assert budget["daily_remaining_lux_hours"] == round(1000 / 365 - 400, 6)
    assert budget["annual_remaining_at_date"] == -200.0


def test_query_intervals_by_day(client):
    case_id = _create_case(client, name="日期过滤柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T20:00:00Z", "end": "2026-03-02T04:00:00Z", "lux": 30},
            {"start": "2026-03-05T08:00:00Z", "end": "2026-03-05T10:00:00Z", "lux": 30},
        ]},
    )
    days = client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-02"
    ).json()
    assert len(days) == 1  # 跨夜区间与 3月2日有交集
    assert days[0]["lux"] == 30

    assert client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-04"
    ).json() == []


def test_naive_datetime_treated_as_utc(client):
    case_id = _create_case(client, name="无时区柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00", "end": "2026-03-01T10:00:00", "lux": 50}
        ]},
    )
    assert r.status_code == 201
    assert r.json()[0]["start"].endswith(("+00:00", "Z"))


def test_move_exhibit_rejects_unknown_target(client):
    c1 = _create_case(client, name="源柜")
    c2 = _create_case(client, name="目标柜")
    ex = client.post(
        f"/api/v1/showcases/{c1}/exhibits",
        json={"name": "陶俑", "sensitivity": "medium"},
    ).json()
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": c2})
    assert r.status_code == 200 and r.json()["showcase_id"] == c2
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": 9999})
    assert r.status_code == 404


def test_all_budgets_and_filter(client):
    c1 = _create_case(client, name="柜A")
    c2 = _create_case(client, name="柜B")
    for cid in (c1, c2):
        client.post(
            f"/api/v1/showcases/{cid}/exhibits",
            json={"name": "铜器", "sensitivity": "low"},
        )
    r = client.get("/api/v1/budgets?day=2026-03-01")
    assert r.status_code == 200 and len(r.json()) == 2

    r = client.get(f"/api/v1/budgets?day=2026-03-01&showcase_id={c1}")
    assert len(r.json()) == 1 and r.json()[0]["showcase_id"] == c1

    assert client.get("/api/v1/budgets?showcase_id=4242").status_code == 404


def test_lighting_preview_basic_and_no_side_effects(client):
    """试算：撤去一盏灯并新增一段照明，对比年度曝光；试算不改库。

    现状区间：08:00-18:00 50 lux；10:00-12:00 200 lux（重叠取 max）
      → 现状年度曝光 = 50*8 + 200*2 = 800 lux·h（上限 1000，不超标）
    试算：撤去 200 lux 区间，新增 次日 08:00-16:00 80 lux
      → 试算曝光 = 50*10 + 80*8 = 1140 lux·h，差额 +340
      → 首日累计 500，次日还需 500，80 lux → 6.25h，即次日 14:15 首次超标
    """
    case_id = _create_case(client, name="试算柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 200},
        ]},
    )
    ids = [iv["id"] for iv in r.json()]
    before = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()

    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={
            "day": "2026-03-01",
            "remove_interval_ids": [ids[1]],
            "add_intervals": [
                {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 80}
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["showcase_id"] == case_id
    assert body["year"] == 2026
    assert body["removed_interval_ids"] == [ids[1]]
    assert body["added_interval_count"] == 1
    item = body["exhibits"][0]
    assert item["current_total_lux_hours"] == 800.0
    assert item["preview_total_lux_hours"] == 1140.0
    assert item["delta_lux_hours"] == 340.0
    assert item["current_first_exceed_at"] is None
    assert item["preview_first_exceed_at"].startswith("2026-03-02T14:15:00")

    # 试算不落库：预算接口结果与试算前完全一致，已存区间不变
    after = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()
    assert after == before
    stored = client.get(f"/api/v1/showcases/{case_id}/intervals").json()
    assert [iv["id"] for iv in stored] == ids


def test_lighting_preview_reject_invalid_remove_ids(client):
    c1 = _create_case(client, name="试算柜甲")
    c2 = _create_case(client, name="试算柜乙")
    r = client.post(
        f"/api/v1/showcases/{c2}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    foreign_id = r.json()[0]["id"]

    # 属于其他展柜的区间 ID → 404
    r = client.post(
        f"/api/v1/showcases/{c1}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [foreign_id], "add_intervals": []},
    )
    assert r.status_code == 404
    # 不存在的区间 ID → 404
    r = client.post(
        f"/api/v1/showcases/{c1}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [999999], "add_intervals": []},
    )
    assert r.status_code == 404
    # 未知展柜 → 404
    r = client.post(
        "/api/v1/showcases/4242/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": []},
    )
    assert r.status_code == 404


def test_lighting_preview_reuses_interval_validation(client):
    case_id = _create_case(client, name="试算校验柜")
    # 负照度 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -1}
        ]},
    )
    assert r.status_code == 422
    # 反向区间 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422


def test_lighting_preview_unlimited_exhibit_and_cross_year(client):
    """不限量展品越限时刻为 null；跨年区间只计入查询年。"""
    case_id = _create_case(client, name="试算不限量柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石雕", "sensitivity": "low"},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            # 跨年区间：仅 2026-01-01 00:00-04:00 这 4 小时计入 2026 年
            {"start": "2025-12-31T20:00:00Z", "end": "2026-01-01T04:00:00Z", "lux": 100},
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-06-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-06-01T00:00:00Z", "end": "2026-06-01T02:00:00Z", "lux": 30}
        ]},
    )
    assert r.status_code == 200, r.text
    item = r.json()["exhibits"][0]
    assert item["annual_limit_lux_hours"] is None
    assert item["current_total_lux_hours"] == 400.0
    assert item["preview_total_lux_hours"] == 460.0
    assert item["delta_lux_hours"] == 60.0
    assert item["current_first_exceed_at"] is None
    assert item["preview_first_exceed_at"] is None


def test_lighting_preview_default_day_is_today(client):
    case_id = _create_case(client, name="试算默认日柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"remove_interval_ids": [], "add_intervals": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["year"] == datetime.now(timezone.utc).year
    assert body["exhibits"] == []


def test_persistence_across_connections(client, tmp_path):
    """同一 DB_PATH 下新建客户端连接，数据仍在（验证 SQLite 落盘）。"""
    case_id = _create_case(client, name="持久化柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "敦煌写经", "sensitivity": "high"},
    )

    import app.main
    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as second:
        r = second.get(f"/api/v1/showcases/{case_id}/exhibits")
        assert r.status_code == 200 and r.json()[0]["name"] == "敦煌写经"


# ------------------------------------------------------------- 安全照度上限

def test_safe_lux_limit_basic(client):
    """已有 500 lux·h（10h×50），计划时段 8h 无灯：500+8L ≤ 1000 → L=62，剩余 4。"""
    case_id = _create_case(client, name="安全上限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["year"] == 2026
    assert body["max_safe_lux"] == 62  # 500+8*62=996 ≤ 1000 < 500+8*63=1004
    assert body["exceeded_exhibits"] == []
    binding = body["binding_exhibits"]
    assert len(binding) == 1
    assert binding[0]["exhibit_name"] == "绢画"
    assert binding[0]["annual_limit_lux_hours"] == 1000.0
    assert binding[0]["remaining_lux_hours"] == 4.0


def test_safe_lux_limit_overlap_takes_max(client):
    """计划时段内已有 100 lux：L≤100 不增曝光；L>100 时 8L ≤ 1000 → L=125，剩余 0。"""
    case_id = _create_case(client, name="安全上限重叠柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "字画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 100}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_safe_lux"] == 125  # 8*125=1000 恰好到限；8*126=1008 超限
    assert body["binding_exhibits"][0]["remaining_lux_hours"] == 0.0


def test_safe_lux_limit_binding_picks_tightest_exhibit(client):
    """多件有限额展品共享时间线：瓶颈为上限最小者，并列时全部返回。"""
    case_id = _create_case(client, name="安全上限多展品柜")
    for name, limit in [("甲", 2000), ("乙", 1000), ("丙", 1000)]:
        client.post(
            f"/api/v1/showcases/{case_id}/exhibits",
            json={"name": name, "sensitivity": "medium", "annual_limit_lux_hours": limit},
        )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_safe_lux"] == 62  # 受上限最小的 1000 约束
    names = [e["exhibit_name"] for e in body["binding_exhibits"]]
    assert names == ["乙", "丙"]  # 甲剩余 1004，不在瓶颈列表
    assert all(e["remaining_lux_hours"] == 4.0 for e in body["binding_exhibits"])


def test_safe_lux_limit_no_limited_exhibits(client):
    """无有限额展品：直接返回协议上限 100000 lux 与空展品列表。"""
    case_id = _create_case(client, name="安全上限不限量柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石雕", "sensitivity": "low"},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["max_safe_lux"] == 100000
    assert body["binding_exhibits"] == []
    assert body["exceeded_exhibits"] == []


def test_safe_lux_limit_already_exceeded(client):
    """现状已超限：不可行，报告超限展品与负剩余额度，不给出安全上限。"""
    case_id = _create_case(client, name="安全上限超限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "丝帛", "sensitivity": "high", "annual_limit_lux_hours": 500},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "陶俑", "sensitivity": "medium", "annual_limit_lux_hours": 100000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T12:00:00Z", "end": "2026-03-01T13:00:00Z", "lux": 100},
        ]},
    )
    # 现状年度曝光 = 50*4 + 100*1 + 50*5 = 550 > 500
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is False
    assert body["max_safe_lux"] is None
    assert body["binding_exhibits"] == []
    exceeded = body["exceeded_exhibits"]
    assert [e["exhibit_name"] for e in exceeded] == ["丝帛"]  # 陶俑未超限
    assert exceeded[0]["remaining_lux_hours"] == -50.0


def test_safe_lux_limit_exactly_at_limit_allows_zero(client):
    """现状恰好到限不算超限：可追加照度为 0，瓶颈展品剩余额度为 0。"""
    case_id = _create_case(client, name="安全上限到限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "纸本", "sensitivity": "high", "annual_limit_lux_hours": 500},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["max_safe_lux"] == 0
    assert body["binding_exhibits"][0]["remaining_lux_hours"] == 0.0


def test_safe_lux_limit_validation(client):
    """反向 / 零长度 / 跨年（含时区归一化后跨年）→ 422；未知展柜 → 404。"""
    case_id = _create_case(client, name="安全上限校验柜")
    url = f"/api/v1/showcases/{case_id}/safe-lux-limit"

    # 反向区间
    r = client.post(url, json={
        "start": "2026-03-02T16:00:00Z", "end": "2026-03-02T08:00:00Z"})
    assert r.status_code == 422 and "结束时间" in r.text
    # 零长度区间
    r = client.post(url, json={
        "start": "2026-03-02T08:00:00Z", "end": "2026-03-02T08:00:00Z"})
    assert r.status_code == 422
    # UTC 下跨年
    r = client.post(url, json={
        "start": "2026-12-31T20:00:00Z", "end": "2027-01-01T04:00:00Z"})
    assert r.status_code == 422 and "同一公历年" in r.text
    # 带时区提交，归一化到 UTC 后跨年：2026-01-01 00:30 (+08:00) → UTC 2025-12-31 16:30
    r = client.post(url, json={
        "start": "2026-01-01T00:30:00+08:00", "end": "2026-06-01T00:00:00Z"})
    assert r.status_code == 422 and "同一公历年" in r.text
    # 未知展柜
    r = client.post("/api/v1/showcases/4242/safe-lux-limit", json={
        "start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"})
    assert r.status_code == 404


def test_safe_lux_limit_read_only_and_time_normalized(client):
    """naive 时间视为 UTC、带时区时间归一化为 UTC 返回；全程只读不改库。"""
    case_id = _create_case(client, name="安全上限只读柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    before_budget = client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()
    before_intervals = client.get(f"/api/v1/showcases/{case_id}/intervals").json()

    # naive 起 + 带时区止：2026-03-03 00:00 (+08:00) → UTC 2026-03-02 16:00
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00", "end": "2026-03-03T00:00:00+08:00"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"].startswith("2026-03-02T08:00:00")
    assert body["end"].startswith("2026-03-02T16:00:00")
    assert body["start"].endswith(("+00:00", "Z"))
    assert body["end"].endswith(("+00:00", "Z"))
    assert body["max_safe_lux"] == 62  # 与 UTC 08:00-16:00 场景一致

    # 只读：预算与已存区间均不变
    assert client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json() == before_budget
    assert client.get(f"/api/v1/showcases/{case_id}/intervals").json() == before_intervals


# ---------------------------------------------------------- 开放时段照明覆盖巡检

def _post_coverage(client, case_id, **overrides):
    payload = {
        "start_date": "2026-03-01",
        "end_date": "2026-03-01",
        "open_start": "08:00",
        "open_end": "18:00",
        "min_lux": 50,
    }
    payload.update(overrides)
    return client.post(f"/api/v1/showcases/{case_id}/open-hours-coverage", json=payload)


def test_open_hours_coverage_basic(client):
    """开放窗口 08:00-18:00、门槛 50 lux。

    区间：08:00-12:00 @50（达标）、10:00-11:00 @200（重叠取 max）、14:00-16:00 @30（不达标）
    合并后 08:00-12:00 全程 ≥50 → 达标 4h；12:00 之后无灯与低照度连续 → 单一缺口 12:00-18:00。
    """
    case_id = _create_case(client, name="巡检柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T11:00:00Z", "lux": 200},
            {"start": "2026-03-01T14:00:00Z", "end": "2026-03-01T16:00:00Z", "lux": 30},
        ]},
    )
    assert r.status_code == 201, r.text

    r = _post_coverage(client, case_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["showcase_id"] == case_id
    assert body["showcase_name"] == "巡检柜"
    assert body["start_date"] == "2026-03-01"
    assert body["end_date"] == "2026-03-01"
    assert body["min_lux"] == 50.0
    assert len(body["days"]) == 1
    day = body["days"][0]
    assert day["date"] == "2026-03-01"
    assert day["open_hours"] == 10.0
    assert day["compliant_hours"] == 4.0
    assert day["compliant_ratio"] == 0.4
    assert len(day["gaps"]) == 1
    assert day["gaps"][0]["start"].startswith("2026-03-01T12:00:00")
    assert day["gaps"][0]["end"].startswith("2026-03-01T18:00:00")
    assert day["gaps"][0]["start"].endswith(("+00:00", "Z"))


def test_open_hours_coverage_no_lights_at_all(client):
    """完全无灯：每天整个开放窗口是一个缺口，达标 0 小时、比例 0。"""
    case_id = _create_case(client, name="无灯巡检柜")
    r = _post_coverage(client, case_id, start_date="2026-03-01", end_date="2026-03-02")
    assert r.status_code == 200, r.text
    days = r.json()["days"]
    assert [d["date"] for d in days] == ["2026-03-01", "2026-03-02"]
    for day in days:
        assert day["compliant_hours"] == 0.0
        assert day["compliant_ratio"] == 0.0
        assert len(day["gaps"]) == 1
        assert day["gaps"][0]["start"] == day["open_start"]
        assert day["gaps"][0]["end"] == day["open_end"]


def test_open_hours_coverage_cross_midnight_interval(client):
    """跨日区间 03-01 20:00 → 03-02 10:00 @80 lux，开放窗口 08:00-12:00。

    03-01 窗口与区间无交集 → 全窗口缺口；03-02 08:00-10:00 被覆盖（达标 2h）。
    """
    case_id = _create_case(client, name="跨日巡检柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T20:00:00Z", "end": "2026-03-02T10:00:00Z", "lux": 80},
        ]},
    )
    r = _post_coverage(
        client, case_id,
        start_date="2026-03-01", end_date="2026-03-02",
        open_start="08:00", open_end="12:00",
    )
    assert r.status_code == 200, r.text
    day1, day2 = r.json()["days"]
    assert day1["compliant_hours"] == 0.0
    assert len(day1["gaps"]) == 1
    assert day1["gaps"][0]["start"].startswith("2026-03-01T08:00:00")
    assert day2["compliant_hours"] == 2.0
    assert day2["compliant_ratio"] == 0.5
    assert len(day2["gaps"]) == 1
    assert day2["gaps"][0]["start"].startswith("2026-03-02T10:00:00")
    assert day2["gaps"][0]["end"].startswith("2026-03-02T12:00:00")


def test_open_hours_coverage_exact_threshold_is_compliant(client):
    """照度恰好等于门槛视为达标：50 lux ≥ 50 → 无缺口，达标比例 1。"""
    case_id = _create_case(client, name="到限巡检柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
        ]},
    )
    r = _post_coverage(client, case_id, min_lux=50)
    assert r.status_code == 200, r.text
    day = r.json()["days"][0]
    assert day["compliant_hours"] == 10.0
    assert day["compliant_ratio"] == 1.0
    assert day["gaps"] == []


def test_open_hours_coverage_overlap_takes_max(client):
    """重叠取最大照度：背景 30 lux 全程 + 重点 100 lux 10:00-12:00。

    仅 10:00-12:00 达标（2h）；缺口为 08:00-10:00 与 12:00-18:00 两段。
    """
    case_id = _create_case(client, name="重叠巡检柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 30},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 100},
        ]},
    )
    r = _post_coverage(client, case_id)
    assert r.status_code == 200, r.text
    day = r.json()["days"][0]
    assert day["compliant_hours"] == 2.0
    assert day["compliant_ratio"] == 0.2
    assert [(g["start"], g["end"]) for g in day["gaps"]] == [
        (day["open_start"], "2026-03-01T10:00:00Z"),
        ("2026-03-01T12:00:00Z", day["open_end"]),
    ]


def test_open_hours_coverage_validation(client):
    """非法范围 → 422；未知展柜 → 404。"""
    case_id = _create_case(client, name="巡检校验柜")

    # 结束日期早于开始日期
    r = _post_coverage(client, case_id, start_date="2026-03-05", end_date="2026-03-01")
    assert r.status_code == 422 and "结束日期" in r.text
    # 超过 7 天（03-01 ~ 03-08 共 8 天）
    r = _post_coverage(client, case_id, start_date="2026-03-01", end_date="2026-03-08")
    assert r.status_code == 422 and "7 天" in r.text
    # 恰好 7 天（含两端）→ 通过
    r = _post_coverage(client, case_id, start_date="2026-03-01", end_date="2026-03-07")
    assert r.status_code == 200, r.text
    assert len(r.json()["days"]) == 7
    # 开放结束不晚于开始（含跨午夜形态 18:00→08:00 与零长度）
    r = _post_coverage(client, case_id, open_start="18:00", open_end="08:00")
    assert r.status_code == 422 and "结束时刻" in r.text
    r = _post_coverage(client, case_id, open_start="08:00", open_end="08:00")
    assert r.status_code == 422
    # 最低照度必须大于 0
    r = _post_coverage(client, case_id, min_lux=0)
    assert r.status_code == 422
    r = _post_coverage(client, case_id, min_lux=-5)
    assert r.status_code == 422
    # 未知展柜
    r = _post_coverage(client, 4242)
    assert r.status_code == 404


def test_open_hours_coverage_read_only_and_tz_normalized(client):
    """带时区的开放时刻归一化为 UTC；巡检只读，预算与已存区间不变。"""
    case_id = _create_case(client, name="巡检只读柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
        ]},
    )
    before_budget = client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()
    before_intervals = client.get(f"/api/v1/showcases/{case_id}/intervals").json()

    # 开放时刻 10:00 (+02:00) → UTC 08:00；20:00 (+02:00) → UTC 18:00
    r = _post_coverage(client, case_id, open_start="10:00:00+02:00", open_end="20:00:00+02:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["open_start"] == "08:00:00"
    assert body["open_end"] == "18:00:00"
    day = body["days"][0]
    assert day["open_start"].startswith("2026-03-01T08:00:00")
    assert day["compliant_hours"] == 10.0
    assert day["gaps"] == []

    # 只读：预算与已存区间均不变
    assert client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json() == before_budget
    assert client.get(f"/api/v1/showcases/{case_id}/intervals").json() == before_intervals
