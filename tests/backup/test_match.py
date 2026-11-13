"""Size-then-hash match: files already in an adopted backup are linked, never copied again."""

from sift.backup.adopt import adopt
from sift.backup.problems import open_problems
from sift.backup.replicator import Dest
from sift.backup.run import mac_target
from tests.backup.test_run import ENABLED_SMALL, Rig, finished, rig, tree  # noqa: F401


def phone_bytes(rig: Rig, path: str) -> bytes:
    file = rig.transport.phone.files[path]
    return rig.transport.phone.read_range(path, 0, file.size)


def camera_files(rig: Rig) -> list[str]:
    return sorted(p for p in rig.transport.phone.files if p.startswith("/sdcard/DCIM/Camera/"))


def adopt_old_folder(rig: Rig, copies: dict[str, bytes]) -> None:
    target = mac_target(rig.ctx.db, rig.ctx.config)
    for rel, data in copies.items():
        path = target.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    adopt(rig.ctx.db, Dest(target.dest_id, "mac", target.root))


def test_files_in_an_adopted_folder_are_linked_by_hash_without_a_transfer(rig: Rig) -> None:
    cams = camera_files(rig)[:3]
    # the old copies sit under other names, sorted by date: names are never the proof
    adopt_old_folder(rig, {f"Old/2023/{i}.jpg": phone_bytes(rig, p) for i, p in enumerate(cams)})
    before = tree(rig.root)
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Camera"]))
    reads = {e.path for e in rig.transport.journal if e.op == "open_read"}
    assert not reads & set(cams)  # no transfer for the matched files
    assert all(tree(rig.root)[k] == v for k, v in before.items())  # old copies untouched
    with rig.ctx.db.read() as conn:
        states = dict(
            conn.execute(
                "SELECT remote_path, state FROM phone_file WHERE remote_path IN (?, ?, ?)", cams
            ).fetchall()
        )
    assert set(states.values()) == {"done"}


def test_same_name_and_size_but_different_bytes_is_never_linked(rig: Rig) -> None:
    cam = camera_files(rig)[0]
    real = phone_bytes(rig, cam)
    fake = bytes(b ^ 0xFF for b in real)  # same size, other bytes
    label_path = f"Phone/{cam.removeprefix('/sdcard/')}"
    adopt_old_folder(rig, {label_path: fake})
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Camera"]))
    files = tree(rig.root)
    assert files[label_path] == fake  # the old copy stays
    assert real in files.values()  # the phone's file was copied, beside it
    kinds = [(p.kind, p.rel_path) for p in open_problems(rig.ctx.db)]
    assert ("older_copy_differs", label_path) in kinds


def test_matching_asks_the_phone_in_batches(rig: Rig) -> None:
    cams = camera_files(rig)
    adopt_old_folder(rig, {f"Old/{i}.jpg": phone_bytes(rig, p) for i, p in enumerate(cams)})
    calls: list[int] = []
    real = rig.transport.sha256

    def counting(paths: list[str]) -> dict[str, str | None]:
        calls.append(len(paths))
        return real(paths)

    rig.transport.sha256 = counting  # type: ignore[method-assign]
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Camera"]))
    assert len(cams) > 1 and len(calls) < len(cams) and max(calls) > 1
