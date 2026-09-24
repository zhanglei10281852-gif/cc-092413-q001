from __future__ import annotations

import json

import pytest

from app.database import get_connection, transaction
from app.services.residents import Operator, ResidentArchiveService


OPERATOR_HEADERS = {"X-Operator": "clerk-zhaoliu"}


def create_resident(client, id_card: str = "110101199001011234", name: str = "张三") -> int:
    response = client.post(
        "/residents",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1990-01-01",
              "phone": "13800000000", "address": "幸福路一号", "village": "幸福村"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_affair(client, resident_id: int, title: str = "社保材料补录") -> int:
    response = client.post(
        "/affairs",
        json={"title": title, "category": "社保", "applicant_id": resident_id, "description": "补录材料"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def audit_events(**filters) -> list[dict]:
    connection = get_connection()
    sql = "SELECT * FROM audit_events"
    conditions = ["1=1"]
    params: list = []
    for key, value in filters.items():
        conditions.append(f"{key}=?")
        params.append(value)
    rows = connection.execute(sql + " WHERE " + " AND ".join(conditions) + " ORDER BY id", params).fetchall()
    return [dict(row) for row in rows]


def test_delete_with_affairs_returns_stable_conflict(client):
    resident_id = create_resident(client)
    affair_id = create_affair(client, resident_id)

    response = client.delete(f"/residents/{resident_id}", headers=OPERATOR_HEADERS)

    assert response.status_code == 409
    body = response.json()
    error = body["error"]
    assert error["code"] == "resident_has_affairs"
    assert error["context"]["resident_id"] == resident_id
    assert error["context"]["resident_name"] == "张三"
    assert error["context"]["affair_count"] == 1
    blocking = error["context"]["blocking_affairs"]
    assert [item["id"] for item in blocking] == [affair_id]
    assert blocking[0]["title"] == "社保材料补录"
    assert "archive" in error["context"]["safe_action"]


def test_conflicted_delete_preserves_resident_and_affairs(client):
    resident_id = create_resident(client)
    affair_id = create_affair(client, resident_id)

    assert client.delete(f"/residents/{resident_id}", headers=OPERATOR_HEADERS).status_code == 409

    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM affairs WHERE id=?", (affair_id,)).fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # 冲突后接口仍可继续办理该事务
    detail = client.get(f"/affairs/{affair_id}")
    assert detail.status_code == 200
    assert detail.json()["applicant_id"] == resident_id


def test_conflicted_delete_writes_denied_audit_with_operator_and_resident_id(client):
    resident_id = create_resident(client)
    create_affair(client, resident_id)

    client.delete(f"/residents/{resident_id}", headers=OPERATOR_HEADERS)

    events = audit_events(action="resident.delete", resource_id=str(resident_id))
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "denied"
    assert event["actor_name"] == "clerk-zhaoliu"
    assert event["resource_type"] == "resident"
    metadata = json.loads(event["metadata_json"])
    assert metadata["affair_count"] == 1


def test_delete_resident_without_affairs_succeeds_and_audits(client):
    resident_id = create_resident(client)

    response = client.delete(f"/residents/{resident_id}", headers=OPERATOR_HEADERS)

    assert response.status_code == 200
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 0
    events = audit_events(action="resident.delete", resource_id=str(resident_id))
    assert len(events) == 1
    assert events[0]["outcome"] == "success"
    assert events[0]["actor_name"] == "clerk-zhaoliu"


def test_delete_unknown_resident_is_404(client):
    response = client.delete("/residents/9999", headers=OPERATOR_HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_delete_requires_operator_header(client):
    resident_id = create_resident(client)
    response = client.delete(f"/residents/{resident_id}")
    assert response.status_code == 422


def test_in_transaction_recheck_blocks_race_and_rolls_back(client):
    resident_id = create_resident(client)
    # 预检时没有事务，此时没有关联事务
    ResidentArchiveService(get_connection()).assert_deletable(resident_id, Operator("赵六"))
    # 预检之后、删除事务之前出现关联事务
    create_affair(client, resident_id)

    with pytest.raises(Exception):
        with transaction(immediate=True) as connection:
            ResidentArchiveService(connection).delete_resident(resident_id, Operator("赵六"))

    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)).fetchone()[0] == 1


def test_archive_requires_explicit_confirmation(client):
    resident_id = create_resident(client)
    create_affair(client, resident_id)

    response = client.post(
        f"/residents/{resident_id}/archive",
        json={"reason": "重复档案合并", "confirm": False},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422

    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)).fetchone()[0] == 1


def test_archive_requires_operator_header(client):
    resident_id = create_resident(client)
    response = client.post(f"/residents/{resident_id}/archive", json={"confirm": True})
    assert response.status_code == 422


def test_archive_safe_path_snapshots_and_removes_linked_data(client):
    resident_id = create_resident(client)
    first_affair = create_affair(client, resident_id, "户籍迁移")
    second_affair = create_affair(client, resident_id, "医保参保")

    response = client.post(
        f"/residents/{resident_id}/archive",
        json={"reason": "震后重复档案合并清理", "confirm": True},
        headers=OPERATOR_HEADERS,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resident_id"] == resident_id
    assert result["archived_affairs"] == 2
    archive_id = result["archive_id"]

    connection = get_connection()
    # 活动数据已移除
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # 归档快照保留居民与每笔事务
    archive = connection.execute("SELECT * FROM resident_archives WHERE id=?", (archive_id,)).fetchone()
    assert archive is not None
    archive = dict(archive)
    assert archive["resident_id"] == resident_id
    assert archive["resident_name"] == "张三"
    assert archive["archived_by"] == "clerk-zhaoliu"
    assert archive["affair_count"] == 2
    assert "110101199001011234" in archive["snapshot_json"]
    archived_rows = connection.execute(
        "SELECT affair_id, snapshot_json FROM affair_archives WHERE archive_id=? ORDER BY affair_id",
        (archive_id,),
    ).fetchall()
    assert [row["affair_id"] for row in archived_rows] == [first_affair, second_affair]
    assert all("户籍迁移" in row["snapshot_json"] or "医保参保" in row["snapshot_json"] for row in archived_rows)

    events = audit_events(action="resident.archive", resource_id=str(resident_id))
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "success"
    assert event["actor_name"] == "clerk-zhaoliu"
    metadata = json.loads(event["metadata_json"])
    assert metadata["archive_id"] == archive_id
    assert metadata["archived_affair_count"] == 2
    assert metadata["resident_id"] == resident_id
    after = json.loads(event["after_json"])
    assert after["archive_id"] == archive_id


def test_archive_failure_leaves_no_half_deleted_state(client, monkeypatch):
    resident_id = create_resident(client)
    create_affair(client, resident_id)

    def boom(self, *args, **kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(
        "app.services.residents.AuditService.record",
        boom,
    )

    with pytest.raises(RuntimeError):
        with transaction(immediate=True) as connection:
            ResidentArchiveService(connection).archive_resident(
                resident_id, Operator("赵六"), reason="重复档案", confirm=True
            )

    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM residents WHERE id=?", (resident_id,)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM resident_archives WHERE resident_id=?", (resident_id,)).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM affair_archives").fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_archive_unknown_resident_is_404(client):
    response = client.post(
        "/residents/9999/archive",
        json={"confirm": True},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
