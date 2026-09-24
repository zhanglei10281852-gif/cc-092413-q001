from __future__ import annotations

import json

from app.database import get_connection


def create_resident(client, id_card="110101199001011234"):
    response = client.post(
        "/residents",
        json={"name": "张三", "id_card": id_card, "gender": "男", "birth_date": "1990-01-01", "phone": "13800000000", "address": "幸福路一号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, resident_id, title="低保申请"):
    response = client.post("/affairs", json={"title": title, "category": "低保", "applicant_id": resident_id, "description": "灾后救助"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def audit_events(action=None, outcome=None):
    connection = get_connection()
    conditions = []
    params = []
    if action is not None:
        conditions.append("action=?")
        params.append(action)
    if outcome is not None:
        conditions.append("outcome=?")
        params.append(outcome)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = connection.execute(f"SELECT * FROM audit_events{where} ORDER BY id", tuple(params)).fetchall()
    return [dict(row) for row in rows]


def foreign_key_violations():
    return [dict(row) for row in get_connection().execute("PRAGMA foreign_key_check").fetchall()]


def test_delete_resident_without_affairs_succeeds_and_audits(client):
    resident_id = create_resident(client)
    response = client.delete(f"/residents/{resident_id}", params={"operator": "档案员甲"})
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "删除成功"
    assert client.get(f"/residents/{resident_id}").status_code == 404

    events = audit_events(action="resident.delete", outcome="success")
    assert len(events) == 1
    event = events[0]
    assert event["resource_type"] == "resident"
    assert event["resource_id"] == str(resident_id)
    assert event["actor_name"] == "档案员甲"
    before = json.loads(event["before_json"])
    assert before["id"] == resident_id
    assert before["id_card"] == "110101199001011234"
    assert foreign_key_violations() == []


def test_delete_resident_with_affairs_returns_stable_conflict_and_preserves_data(client):
    resident_id = create_resident(client)
    first = create_affair(client, resident_id, "低保申请")
    second = create_affair(client, resident_id, "临时救助")

    response = client.delete(f"/residents/{resident_id}")
    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["error"]["code"] == "resident_has_affairs"
    context = payload["error"]["context"]
    assert context["resident_id"] == resident_id
    assert context["affair_count"] == 2
    assert context["affair_ids"] == [first, second]
    assert [item["id"] for item in context["blocking_affairs"]] == [first, second]

    # 关联数据与居民档案都保持原状
    assert client.get(f"/residents/{resident_id}").status_code == 200
    assert client.get(f"/affairs/{first}").status_code == 200
    assert client.get(f"/affairs/{second}").status_code == 200
    assert foreign_key_violations() == []

    # 拒绝动作写入审计并携带操作者与档案标识
    denied = audit_events(action="resident.delete", outcome="denied")
    assert len(denied) == 1
    event = denied[0]
    assert event["resource_id"] == str(resident_id)
    assert event["actor_name"]
    metadata = json.loads(event["metadata_json"])
    assert metadata["blocking_affair_ids"] == [first, second]
    assert metadata["id_card"] == "110101199001011234"


def test_archive_cleanup_removes_affairs_and_resident_atomically(client):
    resident_id = create_resident(client)
    first = create_affair(client, resident_id, "低保申请")
    second = create_affair(client, resident_id, "临时救助")

    response = client.delete(f"/residents/{resident_id}", params={"archive": "true", "operator": "档案员乙"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["removed_affair_count"] == 2
    assert payload["removed_affair_ids"] == [first, second]

    assert client.get(f"/residents/{resident_id}").status_code == 404
    assert client.get(f"/affairs/{first}").status_code == 404
    assert client.get(f"/affairs/{second}").status_code == 404
    assert foreign_key_violations() == []

    events = audit_events(action="resident.archive_cleanup", outcome="success")
    assert len(events) == 1
    event = events[0]
    assert event["actor_name"] == "档案员乙"
    assert event["resource_id"] == str(resident_id)
    metadata = json.loads(event["metadata_json"])
    assert metadata["removed_affair_ids"] == [first, second]
    assert metadata["archive_cleanup"] is True


def test_conflict_then_archive_cleanup_leaves_no_partial_state(client):
    resident_id = create_resident(client)
    affair_id = create_affair(client, resident_id)

    conflict = client.delete(f"/residents/{resident_id}")
    assert conflict.status_code == 409
    # 冲突拒绝后居民与事务都未被改动
    assert client.get(f"/residents/{resident_id}").status_code == 200
    assert client.get(f"/affairs/{affair_id}").status_code == 200

    cleaned = client.delete(f"/residents/{resident_id}", params={"archive": "true"})
    assert cleaned.status_code == 200
    assert client.get(f"/residents/{resident_id}").status_code == 404
    assert client.get(f"/affairs/{affair_id}").status_code == 404
    assert foreign_key_violations() == []


def test_delete_missing_resident_returns_404(client):
    response = client.delete("/residents/9999")
    assert response.status_code == 404
    assert response.json()["detail"] == "居民不存在"


def test_audit_events_visible_through_audit_api(client, admin):
    resident_id = create_resident(client)
    affair_id = create_affair(client, resident_id)

    assert client.delete(f"/residents/{resident_id}").status_code == 409
    assert client.delete(f"/residents/{resident_id}", params={"archive": "true"}).status_code == 200

    response = client.get("/api/audit", params={"resource_type": "resident", "size": 50}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    actions = {(row["action"], row["outcome"]) for row in rows}
    assert ("resident.delete", "denied") in actions
    assert ("resident.archive_cleanup", "success") in actions
    for row in rows:
        assert row["resource_id"] == str(resident_id)
        assert row["actor_name"]
    assert client.get(f"/affairs/{affair_id}").status_code == 404


def test_resident_queries_and_affair_processing_still_work(client):
    resident_id = create_resident(client)
    department = client.post("/departments", json={"name": "综合服务中心", "manager": "李主任", "phone": "010-12345678"})
    assert department.status_code == 201
    department_id = department.json()["id"]

    affair_id = create_affair(client, resident_id)
    processing = client.put(f"/affairs/{affair_id}/process", json={"status": "办理中", "department_id": department_id, "handler": "王经办"})
    assert processing.status_code == 200
    completed = client.put(f"/affairs/{affair_id}/process", json={"status": "已办结", "handler": "王经办", "result": "完成"})
    assert completed.status_code == 200

    listing = client.get("/residents", params={"village": "幸福村"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    detail = client.get(f"/affairs/{affair_id}")
    assert detail.json()["status"] == "已办结"
