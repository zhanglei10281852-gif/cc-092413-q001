from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.core.clock import Clock, SystemClock
from app.core.errors import ConflictError, NotFoundError, ResidentHasAffairsError
from app.repositories.business import ResidentRepository
from app.services.audit import AuditContext, AuditService

DEFAULT_OPERATOR = "未登记操作员"


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


class ResidentService:
    """居民档案删除与归档清理的领域服务。

    删除是受限业务操作：存在关联事务的居民默认拒绝删除并返回稳定的冲突信息；
    只有显式选择归档清理（archive=True）才会在同一事务中移除关联事务与居民档案，
    任何失败都会整体回滚，关键动作（成功、拒绝、失败）都会写入审计。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.residents = ResidentRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def blocking_affairs(self, resident_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id, title, category, status FROM affairs WHERE applicant_id=? ORDER BY id",
            (resident_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, resident_id: int, *, archive: bool, actor: AuditContext) -> dict:
        resident = self.residents.get(resident_id)
        if resident is None:
            raise NotFoundError("居民不存在")
        action = "resident.archive_cleanup" if archive else "resident.delete"
        try:
            with _immediate_transaction(self.connection):
                blockers = self.blocking_affairs(resident_id)
                if blockers and not archive:
                    raise ResidentHasAffairsError(
                        f"居民仍关联 {len(blockers)} 笔事务，无法删除；请先办结相关事务，或明确执行归档清理",
                        context={
                            "resident_id": resident_id,
                            "affair_count": len(blockers),
                            "affair_ids": [row["id"] for row in blockers],
                            "blocking_affairs": blockers,
                        },
                    )
                removed_affair_ids = [row["id"] for row in blockers]
                if archive and blockers:
                    self.connection.execute("DELETE FROM affairs WHERE applicant_id=?", (resident_id,))
                self.connection.execute("DELETE FROM residents WHERE id=?", (resident_id,))
                self.audit.record(
                    actor,
                    action=action,
                    resource_type="resident",
                    resource_id=resident_id,
                    outcome="success",
                    before={"resident": resident, "affairs": blockers} if archive else resident,
                    metadata={
                        "archive_cleanup": archive,
                        "removed_affair_ids": removed_affair_ids,
                        "id_card": resident["id_card"],
                    },
                )
        except ResidentHasAffairsError as exc:
            # 事务已回滚，居民与事务数据保持原状；拒绝动作单独留痕，不随回滚丢失。
            self.audit.record(
                actor,
                action="resident.delete",
                resource_type="resident",
                resource_id=resident_id,
                outcome="denied",
                before=resident,
                metadata={
                    "reason": "affairs_blocking",
                    "blocking_affair_ids": exc.context.get("affair_ids", []),
                    "id_card": resident["id_card"],
                },
            )
            raise
        except sqlite3.IntegrityError:
            # 事务已整体回滚，不会留下半删除状态。
            self.audit.record(
                actor,
                action=action,
                resource_type="resident",
                resource_id=resident_id,
                outcome="failure",
                before=resident,
                metadata={"archive_cleanup": archive, "id_card": resident["id_card"]},
            )
            raise ConflictError("删除失败，关联数据未发生变化，请核对后重试")
        if archive:
            return {
                "message": "归档清理完成",
                "removed_affair_count": len(removed_affair_ids),
                "removed_affair_ids": removed_affair_ids,
            }
        return {"message": "删除成功"}
