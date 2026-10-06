"""Storage hygiene for the raw-document store (FR-183, DR-102, NFR-103).

The egress layer stores every fetched body once, keyed by content hash, and
registers it in ``raw_document`` so extractors can be re-run later (FR-183,
DR-102).  Nothing ever removed one.  A weekly revalidation of ~6,900 ATS boards
changes a few hundred of them, and every changed board leaves the *previous*
body behind: ~160 MB a week, ~8 GB a year, referenced by no provenance row, no
vacancy and no live cache entry (docs/Data_Gathering_Plan.md section 4, L2, and
section 5.2 item N7).

A document is an orphan when all four of these hold:

* it is older than the retention window (30 days by default) - so a document a
  running campaign has just fetched is never a candidate;
* no ``provenance`` row points at it - which is the FR-166 trail from a
  collected record back to the page it came from;
* no ``vacancy`` and no ``compensation_observation`` row points at it directly;
* no *unexpired* ``http_cache`` entry is serving its file as a cached body -
  deleting the file underneath a live cache entry would turn a cache hit into a
  silent miss.

The last check is done here rather than in SQL because ``http_cache.body_path``
holds an absolute path while ``raw_document.storage_path`` is relative to the
data directory; the file name is the content hash, which is what the two have
in common and what makes the comparison exact.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import query_all, write_tx

log = logging.getLogger(__name__)

#: One sweep never deletes more than this, so a first sweep over a long-lived
#: database cannot hold the single writer (CR-408) for minutes.
DEFAULT_SWEEP_LIMIT = 2_000

_ORPHANS = """
SELECT r.id AS id, r.storage_path AS storage_path, r.content_hash AS content_hash,
       r.byte_size AS byte_size
FROM raw_document r
WHERE r.fetched_at < ?
  AND NOT EXISTS (SELECT 1 FROM provenance p WHERE p.raw_document_id = r.id)
  AND NOT EXISTS (SELECT 1 FROM vacancy v WHERE v.raw_document_id = r.id)
  AND NOT EXISTS (
      SELECT 1 FROM compensation_observation c WHERE c.raw_document_id = r.id
  )
ORDER BY r.fetched_at
LIMIT ?
"""

_LIVE_CACHE_BODIES = "SELECT body_path FROM http_cache WHERE expires_at > ? AND body_path IS NOT NULL"


def orphan_raw_documents(
    cutoff: str, now: str, limit: int = DEFAULT_SWEEP_LIMIT
) -> list[dict[str, Any]]:
    """Raw documents older than ``cutoff`` that nothing references any more.

    ``now`` is the moment against which ``http_cache.expires_at`` is compared,
    so a body that is still being served from the cache is kept whatever its
    age (FR-182).
    """
    candidates = query_all(_ORPHANS, (cutoff, int(limit)))
    if not candidates:
        return []
    # `body_path` is written with the host's own separator, so on Windows every
    # stored path uses backslashes.  Splitting on "/" alone left `live` full of
    # whole path strings, which never match a content hash: the set was
    # effectively empty and every body past the retention window was deleted as
    # an orphan while an unexpired http_cache row was still serving it.
    live = {
        str(row["body_path"]).replace("\\", "/").rsplit("/", 1)[-1].split(".")[0]
        for row in query_all(_LIVE_CACHE_BODIES, (now,))
    }
    return [row for row in candidates if str(row["content_hash"]) not in live]


def delete_raw_documents(ids: list[str]) -> int:
    """Delete raw-document rows by id.  The caller removes the files first."""
    if not ids:
        return 0
    deleted = 0
    with write_tx() as conn:
        for chunk in (ids[i : i + 500] for i in range(0, len(ids), 500)):
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(f"DELETE FROM raw_document WHERE id IN ({marks})", tuple(chunk))
            deleted += cur.rowcount or 0
    return deleted
