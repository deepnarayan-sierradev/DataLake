"""
The one S3 Parquet reader (F20).

Three near-identical readers existed: `analytics_publisher`'s materialised the whole prefix into a
list, `serving_store`'s yielded batches, and `transformation`'s yielded records — and the one that
materialised was the one running in a 512 MB Lambda holding a second full copy alongside it. The
duplication was not the cost; the cost was that only one of the three bounded its memory, and
nothing made that visible.

Two entry points, mirroring `dynamodb_paging`:

- `iter_parquet_batches` yields row-group-sized lists. Use it when the consumer is itself batched
  (a database upsert, a paged write).
- `iter_parquet_records` yields one dict at a time, lazily. Use it for a streaming transform, where
  materialising the prefix is the thing being avoided.

Neither ever holds more than one row group. A caller that genuinely needs the whole set writes
`list(...)` and owns that decision explicitly, at the call site, where it is reviewable.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any, Final

import pyarrow.parquet as pq

DEFAULT_BATCH_SIZE: Final[int] = 10_000


def _validated_prefix(prefix: str) -> str:
    """Reject traversal and absolute prefixes before they reach S3 (OWASP A01)."""
    clean = prefix.strip().rstrip("/") + "/"
    if ".." in clean or clean.startswith("/"):
        raise ValueError(f"Unsafe S3 prefix rejected: {clean!r}")
    return clean


def iter_parquet_batches(
    s3: Any,
    bucket: str,
    prefix: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Yield row-group batches of dicts from every `.parquet` object under `prefix`."""
    clean = _validated_prefix(prefix)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=clean):
        for obj in page.get("Contents", []):
            if not str(obj["Key"]).endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buffer = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buffer)
            for record_batch in table.to_batches(max_chunksize=batch_size):
                columns = record_batch.to_pydict()
                names = list(columns.keys())
                yield [
                    {name: columns[name][index] for name in names}
                    for index in range(record_batch.num_rows)
                ]
            # Released per object rather than per prefix: peak memory is one row group, not the
            # sum of every file under the prefix.
            del table


def iter_parquet_records(
    s3: Any,
    bucket: str,
    prefix: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield one record at a time, lazily, never materialising the prefix."""
    for batch in iter_parquet_batches(s3, bucket, prefix, batch_size=batch_size):
        yield from batch
