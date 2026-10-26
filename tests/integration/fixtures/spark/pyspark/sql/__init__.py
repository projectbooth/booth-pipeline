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


class _SparkContext:
    """Records the files shipped to executors (there are none here; the driver imports them itself)."""

    def __init__(self) -> None:
        self.py_files: list[str] = []

    def addPyFile(self, path: str) -> None:  # noqa: N802 - pyspark's own spelling
        self.py_files.append(path)


class SparkSession:
    builder = _Builder()
    sparkContext = _SparkContext()  # noqa: N815 - pyspark's own spelling

    def range(self, start, end=None):
        lo, hi = (0, start) if end is None else (start, end)
        return DataFrame(range(lo, hi))

    def stop(self) -> None:
        pass
