from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.services.audit import AuditContext, AuditService

RESIDENT_HAS_AFFAIRS = "resident_has_affairs"


@dataclass(frozen=True, slots=True)
class Operator:
    name: str

    @classmethod
    def from_header(cls, value: str | None) -> "Operator":
        name = (value or "").strip()
        if not name:
            raise ValidationError("缺少操作者标识，请通过 X-Operator 请求头提供")
        if len(name) > 50:
            raise ValidationError("操作者标识长度不能超过 50 个字符")
        try:
            name.encode("ascii")
        except UnicodeEncodeError:
            raise ValidationError("操作者标识只能使用 ASCII 字符（工号或用户名）")
        return cls(name=name)


class ResidentArchiveService:
    """居民删除与归档清理的领域服务。

    普通删除只允许移除没有关联事务的居民；关联数据只能走显式归档清理。
    所有批量写入都在调用方提供的即时事务内完成，失败整体回滚。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def _get_resident(self, resident_id: int) -> dict:
        resident = self.connection.execute(
            "SELECT * FROM residents WHERE id=?", (resident_id,)
        ).fetchone()
        if resident is None:
            raise NotFoundError("居民不存在", context={"resident_id": resident_id})
        return dict(resident)

    def _referencing_affairs(self, resident_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id, title, category, status, created_at FROM affairs "
            "WHERE applicant_id=? ORDER BY id",
            (resident_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _conflict(self, resident: dict, affairs: list[dict], *, record_denied: bool, operator: Operator) -> None:
        if record_denied:
            self.audit.record(
                AuditContext(actor_user_id=None, actor_name=operator.name),
                action="resident.delete",
                resource_type="resident",
                resource_id=resident["id"],
                outcome="denied",
                before={"id_card": resident["id_card"], "name": resident["name"]},
                metadata={"reason": "存在关联事务", "affair_count": len(affairs)},
            )
        raise ConflictError(
            "该居民仍有关联事务，不能直接删除；请先办结或通过归档清理移除",
            context={
                "resident_id": resident["id"],
                "resident_name": resident["name"],
                "affair_count": len(affairs),
                "blocking_affairs": affairs,
                "safe_action": "POST /residents/{id}/archive",
            },
            code=RESIDENT_HAS_AFFAIRS,
        )

    def assert_deletable(self, resident_id: int, operator: Operator) -> dict:
        """事务前的可预期预检（自动提交）：404 / 409 均在此给出并写入拒绝审计。"""
        resident = self._get_resident(resident_id)
        affairs = self._referencing_affairs(resident_id)
        if affairs:
            self._conflict(resident, affairs, record_denied=True, operator=operator)
        return resident

    def delete_resident(self, resident_id: int, operator: Operator) -> dict:
        """在即时事务中执行删除，并再次校验关联，防止预检后的并发写入。"""
        resident = self._get_resident(resident_id)
        affairs = self._referencing_affairs(resident_id)
        if affairs:
            self._conflict(resident, affairs, record_denied=False, operator=operator)
        cursor = self.connection.execute("DELETE FROM residents WHERE id=?", (resident_id,))
        if cursor.rowcount == 0:
            raise NotFoundError("居民不存在", context={"resident_id": resident_id})
        self.audit.record(
            AuditContext(actor_user_id=None, actor_name=operator.name),
            action="resident.delete",
            resource_type="resident",
            resource_id=resident_id,
            outcome="success",
            before={"id_card": resident["id_card"], "name": resident["name"]},
            metadata={"resident_id": resident_id},
        )
        return {"resident_id": resident_id, "message": "删除成功"}

    def archive_resident(
        self,
        resident_id: int,
        operator: Operator,
        *,
        reason: str | None,
        confirm: bool,
    ) -> dict:
        """显式归档清理：先快照居民与关联事务，再删除活动数据，全部在同一事务内。"""
        if not confirm:
            raise ValidationError("归档清理属于不可逆操作，必须显式提交 confirm=true")
        resident = self._get_resident(resident_id)
        affairs = self._referencing_affairs(resident_id)
        now = to_storage(self.clock.now())
        normalized_reason = (reason or "").strip() or None

        archive_cursor = self.connection.execute(
            "INSERT INTO resident_archives"
            "(resident_id, resident_name, id_card, snapshot_json, affair_count, reason, archived_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                resident_id,
                resident["name"],
                resident["id_card"],
                json.dumps(resident, ensure_ascii=False, sort_keys=True),
                len(affairs),
                normalized_reason,
                operator.name,
                now,
            ),
        )
        archive_id = int(archive_cursor.lastrowid)

        for affair in affairs:
            self.connection.execute(
                "INSERT INTO affair_archives(archive_id, affair_id, resident_id, snapshot_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    archive_id,
                    affair["id"],
                    resident_id,
                    json.dumps(affair, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

        deleted_affairs = self.connection.execute(
            "DELETE FROM affairs WHERE applicant_id=?", (resident_id,)
        ).rowcount
        cursor = self.connection.execute("DELETE FROM residents WHERE id=?", (resident_id,))
        if cursor.rowcount == 0:
            raise NotFoundError("居民不存在", context={"resident_id": resident_id})

        self.audit.record(
            AuditContext(actor_user_id=None, actor_name=operator.name),
            action="resident.archive",
            resource_type="resident",
            resource_id=resident_id,
            outcome="success",
            before={"id_card": resident["id_card"], "name": resident["name"]},
            after={"archive_id": archive_id, "archived_affairs": deleted_affairs},
            metadata={
                "resident_id": resident_id,
                "archive_id": archive_id,
                "archived_affair_count": deleted_affairs,
                "reason": normalized_reason,
            },
        )
        return {
            "resident_id": resident_id,
            "archive_id": archive_id,
            "archived_affairs": deleted_affairs,
            "message": "档案已归档清理",
        }
