# syntax=docker/dockerfile:1

# ---- build: wheels for the module and every dependency, so the runtime image needs no compiler,
# no pip cache and no network.
FROM python:3.13-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime
FROM python:3.13-slim-bookworm
# uid/gid 65532 is the "nonroot" id the chart's securityContext pins (same as every booth-* image).
RUN groupadd --gid 65532 nonroot && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels booth-pipeline && rm -rf /wheels
# The root filesystem is read-only in the chart; /tmp is an emptyDir. Everything that wants to
# write (task working directories, Dagster's scratch space, Python's bytecode cache) must go there.
ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DAGSTER_HOME=/tmp/dagster
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["booth-pipeline"]
