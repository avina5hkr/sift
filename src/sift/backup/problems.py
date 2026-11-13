"""Backup, Problems: what the owner should know about a backed-up file. Sift never deletes it."""

from dataclasses import dataclass

from sift.core.clock import Clock
from sift.core.db import Database


@dataclass(frozen=True)
class Problem:
    id: int
    kind: str
    dest_id: int | None
    dest_kind: str | None
    rel_path: str | None
    content_hash: str | None
    detail: str | None
    created_at: int
    repairable: bool


def add_problem(
    db: Database, clock: Clock, kind: str, dest_id: int, rel_path: str, digest: str, detail: str
) -> None:
    """List a problem once: an open one for the same file is not added again."""
    with db.tx() as conn:
        if conn.execute(
            "SELECT 1 FROM problem WHERE kind = ? AND dest_id = ? AND rel_path = ? "
            "AND resolved_at IS NULL",
            (kind, dest_id, rel_path),
        ).fetchone():
            return
        conn.execute(
            "INSERT INTO problem (kind, dest_id, rel_path, content_hash, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, dest_id, rel_path, digest, detail, clock.now()),
        )


def open_problems(db: Database) -> list[Problem]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT p.id, p.kind, p.dest_id, d.kind, p.rel_path, p.content_hash, p.detail, "
            "p.created_at, EXISTS (SELECT 1 FROM replica r WHERE r.content_hash = p.content_hash "
            "AND r.dest_id != IFNULL(p.dest_id, -1)) FROM problem p "
            "LEFT JOIN destination d ON d.id = p.dest_id WHERE p.resolved_at IS NULL "
            "ORDER BY p.created_at DESC, p.id DESC"
        ).fetchall()
    return [
        Problem(int(r[0]), str(r[1]), r[2], r[3], r[4], r[5], r[6], int(r[7]), bool(r[8]))
        for r in rows
    ]
