"""Stream rows into a string-typed Parquet file in bounded-memory batches."""
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

MAX_BATCH_CELLS = 2_000_000


class ParquetSink:
    def __init__(self, path: Path, columns: list[str]):
        self.path = path
        self.columns = columns
        self.schema = pa.schema([(c, pa.string()) for c in columns])
        self.writer = pq.ParquetWriter(str(path), self.schema, compression="zstd")
        self.batch_rows = max(1000, MAX_BATCH_CELLS // max(1, len(columns)))
        self._buf = [[] for _ in columns]
        self._n = 0
        self.rows = 0

    def write(self, row: list):
        buf = self._buf
        for i, v in enumerate(row):
            buf[i].append(v if v != "" else None)
        self._n += 1
        if self._n >= self.batch_rows:
            self.flush()

    def flush(self):
        if not self._n:
            return
        arrays = [pa.array(b, type=pa.string()) for b in self._buf]
        self.writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
        self.rows += self._n
        self._buf = [[] for _ in self.columns]
        self._n = 0

    def close(self) -> int:
        self.flush()
        self.writer.close()
        return self.rows
