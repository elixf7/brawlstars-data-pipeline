"""Export a season database to Parquet.

SQLite is the working store: its unique index is what makes re-crawling
idempotent, and the crawl frontier and run history live in the same
transactional file. Parquet is what gets published — columnar, compressed, and
readable by anything without a SQLite driver.

The export is a projection, not a move. It carries `matches` and the
skill-feature provenance; the operational tables stay behind.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bsetl.logconfig import get_logger

logger = get_logger(__name__)

#: Tables that describe the data. Everything else is pipeline bookkeeping.
PUBLISHED_TABLES = ("matches", "skill_bin_metadata")
#: Pipeline state, never published: it says nothing about the game and
#: everything about our crawl.
INTERNAL_TABLES = ("fetched_tags", "crawl_frontier", "pipeline_runs")

_SQLITE_TO_ARROW = {
    "TEXT": pa.string(),
    "INTEGER": pa.int64(),
    "REAL": pa.float64(),
}


@dataclass
class ExportResult:
    rows: int
    files: int
    source_bytes: int
    parquet_bytes: int
    partitions: list[str] = field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        return self.source_bytes / self.parquet_bytes if self.parquet_bytes else 0.0


def matches_arrow_schema(conn: sqlite3.Connection) -> pa.Schema:
    """Build the Arrow schema from the table's declared types.

    Declared types, not inferred ones: every column is nullable, and a column
    that happens to be entirely NULL in one season would otherwise infer as
    null-typed and produce a file that will not concatenate with the others.
    """
    fields = []
    for row in conn.execute("PRAGMA table_info(matches)"):
        name, decl = row[1], (row[2] or "TEXT").upper()
        arrow_type = _SQLITE_TO_ARROW.get(decl.split("(")[0])
        if arrow_type is None:
            logger.warning("Unmapped SQLite type %r on column %r; exporting as string",
                           decl, name)
            arrow_type = pa.string()
        fields.append(pa.field(name, arrow_type, nullable=True))
    if not fields:
        raise RuntimeError("No `matches` table found in the source database")
    return pa.schema(fields)


def _partition_key(battle_time: str | None) -> str:
    """YYYY-MM-DD from a battle_time like 20251102T002849.000Z."""
    if not battle_time or len(battle_time) < 8:
        return "unknown"
    d = battle_time[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d.isdigit() else "unknown"


def export_matches_to_parquet(
    db_path: str,
    out_dir: str,
    *,
    batch_size: int = 200_000,
    compression: str = "zstd",
    overwrite: bool = True,
) -> ExportResult:
    """Write `matches` as Parquet partitioned by day.

    Partitioned by battle date because that is how the data is consumed: a
    season is analysed in time slices, and a reader wanting one week should not
    have to scan the whole season.
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {src}")
    out = Path(out_dir)
    if out.exists() and overwrite:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        schema = matches_arrow_schema(conn)
        names = [f.name for f in schema]
        total = int(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0])
        logger.info("Exporting %d rows from %s", total, src.name)

        # One writer per day, kept open across batches so a day that spans
        # batch boundaries lands in a single file rather than many small ones.
        writers: dict[str, pq.ParquetWriter] = {}
        cursor = conn.execute(f"SELECT {', '.join(names)} FROM matches ORDER BY battle_time")
        rows_written = 0
        try:
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                buckets: dict[str, list[tuple]] = {}
                bt = names.index("battle_time")
                for r in rows:
                    buckets.setdefault(_partition_key(r[bt]), []).append(r)

                for day, bucket in buckets.items():
                    table = pa.Table.from_pydict(
                        {n: [r[i] for r in bucket] for i, n in enumerate(names)},
                        schema=schema,
                    )
                    if day not in writers:
                        part = out / f"battle_date={day}"
                        part.mkdir(parents=True, exist_ok=True)
                        writers[day] = pq.ParquetWriter(
                            part / "data.parquet", schema, compression=compression
                        )
                    writers[day].write_table(table)
                    rows_written += len(bucket)
        finally:
            for w in writers.values():
                w.close()
    finally:
        conn.close()

    files = sorted(out.rglob("*.parquet"))
    result = ExportResult(
        rows=rows_written,
        files=len(files),
        source_bytes=src.stat().st_size,
        parquet_bytes=sum(f.stat().st_size for f in files),
        partitions=sorted(p.name.split("=", 1)[1] for p in out.iterdir() if p.is_dir()),
    )
    logger.info(
        "Wrote %d rows into %d file(s), %.1f MB from %.1f MB (%.1fx smaller)",
        result.rows, result.files, result.parquet_bytes / 1e6,
        result.source_bytes / 1e6, result.compression_ratio,
    )
    return result


def export_clean_sqlite(db_path: str, out_path: str) -> int:
    """Copy the database with pipeline state removed, then VACUUM.

    Downstream consumers expect SQLite; they should not receive our crawl
    frontier, fetched-tag ledger, or run history along with it.
    Returns the size of the result in bytes.
    """
    src, dst = Path(db_path), Path(out_path)
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    shutil.copy2(src, dst)
    conn = sqlite3.connect(dst)
    try:
        for table in INTERNAL_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    size = dst.stat().st_size
    logger.info(
        "Clean SQLite export: %.1f MB from %.1f MB", size / 1e6, src.stat().st_size / 1e6
    )
    return size
