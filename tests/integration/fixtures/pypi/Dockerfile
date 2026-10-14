# The in-cluster test package index for tests/integration/pip.sh (ADR 0115). Built FROM the module's
# own CI image, which is already built and loaded into kind and has Python and pip.
FROM booth-pipeline:ci
USER 0:0
COPY . /src
RUN pip download --no-cache-dir --require-hashes --only-binary :all: --no-deps -r /src/wheels.lock -d /srv/index/files \
 && python /src/build_index.py /src /srv/index \
 && chmod -R a+rX /srv/index
USER 65532:65532
ENTRYPOINT ["python", "/src/server.py"]
