"""The orphan sweep must never delete a body an unexpired cache entry serves.

``orphan_raw_documents`` is the only thing that deletes stored bodies, and its
last guard is a set of the content hashes the HTTP cache is still serving.  That
set is built by taking the file name out of ``http_cache.body_path``, which is
written with the host's own separator: on Windows every one of the 1,554 rows in
the installed database uses backslashes and none uses a forward slash.  Splitting
on "/" alone therefore left whole path strings in the set, which never match a
64-character content hash, so the guard was permanently open and every body past
the retention window was a candidate while its cache entry was still live.

The bug had not fired yet only because nothing in that database is 30 days old.
Both separators are checked here so the guard cannot regress on either host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.migrator import migrate

CUTOFF = "2026-09-01T00:00:00+00:00"
NOW = "2026-10-06T00:00:00+00:00"
OLD = "2026-08-01T00:00:00+00:00"   # before CUTOFF: past the retention window
HASH = "a" * 64
OTHER = "b" * 64


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A migrated scratch database every query in this module resolves to."""
    path = tmp_path / "hygiene.db"
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    return path


def _raw_document(content_hash: str, fetched_at: str = OLD) -> None:
    with conn_mod.write_tx() as conn:
        conn.execute(
            "INSERT INTO raw_document (id, url, content_hash, storage_path, byte_size,"
            " fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"rd-{content_hash[:4]}", "https://example.test/a", content_hash,
             f"raw/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.bin", 10, fetched_at),
        )


def _cache_entry(body_path: str, expires_at: str) -> None:
    with conn_mod.write_tx() as conn:
        conn.execute(
            "INSERT INTO http_cache (url_hash, url, status_code, body_path, fetched_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (body_path[-12:], "https://example.test/a", 200, body_path, OLD, expires_at),
        )


@pytest.mark.parametrize(
    "body_path",
    [
        # Windows: what every row of the installed database actually holds.
        rf"D:\Users\x\dreamjob\data\raw\aa\aa\{HASH}.bin",
        # POSIX: the container and WSL write this one.
        f"/srv/dreamjob/data/raw/aa/aa/{HASH}.bin",
    ],
    ids=["windows", "posix"],
)
def test_a_body_served_from_a_live_cache_entry_is_not_an_orphan(db, body_path: str) -> None:
    from dreamjob.db.repositories.hygiene import orphan_raw_documents

    _raw_document(HASH)
    _cache_entry(body_path, expires_at="2026-11-30T00:00:00+00:00")

    assert orphan_raw_documents(CUTOFF, NOW) == []


def test_an_expired_cache_entry_does_not_protect_a_body(db) -> None:
    """The guard is about live cache entries only, or nothing is ever reclaimed."""
    from dreamjob.db.repositories.hygiene import orphan_raw_documents

    _raw_document(HASH)
    _cache_entry(rf"D:\x\data\raw\aa\aa\{HASH}.bin", expires_at="2026-09-15T00:00:00+00:00")

    assert [row["content_hash"] for row in orphan_raw_documents(CUTOFF, NOW)] == [HASH]


def test_a_live_entry_protects_only_its_own_body(db) -> None:
    """A path that cannot be split to a hash must not protect everything either.

    The symptom of the old split was a set of unmatchable strings, which reads
    the same as an empty set.  This holds the other direction: the hash that IS
    being served is kept and the one that is not is swept.
    """
    from dreamjob.db.repositories.hygiene import orphan_raw_documents

    _raw_document(HASH)
    _raw_document(OTHER)
    _cache_entry(rf"D:\x\data\raw\aa\aa\{HASH}.bin", expires_at="2026-11-30T00:00:00+00:00")

    assert [row["content_hash"] for row in orphan_raw_documents(CUTOFF, NOW)] == [OTHER]


def test_a_document_inside_the_retention_window_is_never_a_candidate(db) -> None:
    from dreamjob.db.repositories.hygiene import orphan_raw_documents

    _raw_document(HASH, fetched_at="2026-10-05T00:00:00+00:00")

    assert orphan_raw_documents(CUTOFF, NOW) == []
