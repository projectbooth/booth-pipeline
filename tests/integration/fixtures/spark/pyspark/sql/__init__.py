"""Stub ``pyspark.sql`` (see ../__init__.py)."""


class DataFrame:
    def __init__(self, rows):
        self._rows = list(rows)

    def count(self) -> int:
        return len(self._rows)

    def collect(self):
        return list(self._rows)


class _Builder:
    def appName(self, _name):  # noqa: N802 - pyspark's own spelling
        return self

    def getOrCreate(self):  # noqa: N802
        return SparkSession()


class SparkSession:
    builder = _Builder()

    def range(self, start, end=None):
        lo, hi = (0, start) if end is None else (start, end)
        return DataFrame(range(lo, hi))

    def stop(self) -> None:
        pass
