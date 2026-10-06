"""The task-side s3 helper (runners/_s3.py, ctx.s3, and the SQL harness's automatic secret).

DuckDB doesn't read the endpoint from AWS_CONFIG_FILE, so the helper reads the credential sidecar's
config file and hands DuckDB the location; the keys stay with DuckDB's own credential chain. Both
forms of addressing_style are accepted: the nested `s3 =` form botocore documents, which
booth-core@8f0c6b4 writes (ADR 0095, sixth amendment), and the top-level key 330a178 wrote."""

from __future__ import annotations

import pytest

from booth_pipeline.runners import _s3
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .test_kubejob import Lines

KEYS = "[default]\naws_access_key_id = AKCITEST\naws_secret_access_key = s3cr3t\naws_session_token = tok\n"


def files(tmp_path, config: str | None, credentials: str | None = KEYS) -> dict[str, str]:
    cred, conf = tmp_path / "credentials", tmp_path / "credentials.config"
    if credentials is not None:
        cred.write_text(credentials)
    if config is not None:
        conf.write_text(config)
    return {"AWS_SHARED_CREDENTIALS_FILE": str(cred), "AWS_CONFIG_FILE": str(conf)}


TOP = "[default]\nendpoint_url = http://minio.storage.svc:9000\nregion = us-east-1\naddressing_style = path\n"
NESTED = "[default]\nendpoint_url = https://s3.example.test\nregion = eu-west-1\ns3 =\n    addressing_style = virtual\n"


def test_the_top_level_addressing_style_is_read(tmp_path):
    """What booth-core@330a178 wrote; kept working for a sidecar of that age."""
    loc = _s3.location(files(tmp_path, TOP))
    assert loc == _s3.Location("http://minio.storage.svc:9000", "us-east-1", "path")
    assert (loc.host, loc.scheme) == ("minio.storage.svc:9000", "http")


def test_the_nested_addressing_style_is_read(tmp_path):
    """botocore's form, which booth-core@8f0c6b4 writes (ADR 0095, sixth amendment)."""
    assert _s3.location(files(tmp_path, NESTED)) == _s3.Location("https://s3.example.test", "eu-west-1", "virtual")


def test_nested_wins_when_both_are_present(tmp_path):
    both = "[default]\nendpoint_url = http://m:9000\naddressing_style = virtual\ns3 =\n    addressing_style = path\n"
    assert _s3.location(files(tmp_path, both)).addressing_style == "path"  # the one botocore actually honors


def test_no_section_means_real_aws(tmp_path):
    """The sidecar writes no config for real AWS S3 (no endpoint to set): the engine's own defaults."""
    assert _s3.location(files(tmp_path, "")) == _s3.Location(None, None, None)
    assert _s3.location(files(tmp_path, None)) == _s3.Location(None, None, None)


def test_a_named_profile_uses_the_config_files_profile_section(tmp_path):
    env = files(tmp_path, "[profile lake]\nendpoint_url = http://m:9000\n")
    assert _s3.location({**env, "AWS_PROFILE": "lake"}).endpoint_url == "http://m:9000"


def test_no_variables_means_not_enabled():
    with pytest.raises(_s3.S3Unavailable, match="Platform access"):
        _s3.location({})


def test_no_credentials_file_yet_means_not_ready(tmp_path):
    with pytest.raises(_s3.S3Unavailable, match="first lease"):
        _s3.location(files(tmp_path, TOP, credentials=None))


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (
            _s3.Location("http://minio:9000", "us-east-1", "path"),
            "CREATE OR REPLACE SECRET booth_s3 (TYPE s3, PROVIDER credential_chain, REFRESH auto, REGION 'us-east-1', ENDPOINT 'minio:9000', USE_SSL false, URL_STYLE 'path')",
        ),
        (
            _s3.Location("https://s3.example.test", None, "virtual"),
            "CREATE OR REPLACE SECRET booth_s3 (TYPE s3, PROVIDER credential_chain, REFRESH auto, ENDPOINT 's3.example.test', USE_SSL true, URL_STYLE 'vhost')",
        ),
        (_s3.Location("http://m:9000", None, "auto"), "CREATE OR REPLACE SECRET booth_s3 (TYPE s3, PROVIDER credential_chain, REFRESH auto, ENDPOINT 'm:9000', USE_SSL false)"),
        (_s3.Location(None, None, None), "CREATE OR REPLACE SECRET booth_s3 (TYPE s3, PROVIDER credential_chain, REFRESH auto)"),
    ],
)
def test_the_secret_carries_the_location_only(loc, expected):
    """"virtual" is DuckDB's 'vhost'; "auto" and unstated leave DuckDB's default. No key, ever."""
    assert _s3.secret_sql(loc) == expected


def test_a_value_cannot_break_out_of_its_sql_string():
    sql = _s3.secret_sql(_s3.Location("http://evil'); DROP TABLE x; --:9000", "r'1", None))
    assert "REGION 'r''1'" in sql and "''); DROP" in sql


def test_an_unsafe_secret_name_is_refused():
    with pytest.raises(ValueError, match="letters, digits"):
        _s3.secret_sql(_s3.Location(None, None, None), name="x; DROP")


class FakeCon:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> None:
        self.sql.append(sql)


def test_the_baked_extensions_are_loaded_never_installed(tmp_path):
    """A task pod has no internet: with the image's directory present, only LOAD."""
    con = FakeCon()
    _s3.load_extensions(con, str(tmp_path))
    assert con.sql == [f"SET extension_directory = '{tmp_path}'", "LOAD httpfs; LOAD aws;"]


def test_outside_the_image_they_are_installed_on_first_use(tmp_path):
    con = FakeCon()
    _s3.load_extensions(con, str(tmp_path / "absent"))
    assert con.sql == ["INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;"]


# ---- in a real task subprocess --------------------------------------------------------------------


def run_task(source: str, env: dict[str, str], language: str = "python") -> tuple[object, Lines]:
    lines = Lines()
    inv = TaskInvocation("r", "t", "transform", 1, source, {}, {}, 60, TaskAccess("acme", "tok", "http://s", "http://c"), language=language)
    return SubprocessRunner(task_env=env).run(inv, lines, Cancellation()), lines


def test_ctx_s3_location_in_a_python_task(tmp_path):
    out, _ = run_task("def run(ctx):\n    l = ctx.s3.location()\n    return [l.host, l.addressing_style]\n", files(tmp_path, TOP))
    assert out == ["minio.storage.svc:9000", "path"]


def test_ctx_s3_without_credentials_fails_with_the_fix(tmp_path):
    from booth_pipeline.runners.base import TaskFailed

    lines = Lines()
    with pytest.raises(TaskFailed):
        SubprocessRunner().run(
            TaskInvocation("r", "t", "transform", 1, "def run(ctx):\n    ctx.s3.location()\n", {}, {}, 60, None), lines, Cancellation()
        )
    assert any("Platform access" in m for _, m in lines.out)


def test_a_sql_task_without_s3_is_untouched():
    out, lines = run_task("SELECT 42 AS n", {}, language="sql")
    assert out == [{"n": 42}] and not [m for s, m in lines.out if "s3" in m]


def test_a_sql_task_still_runs_when_s3_is_not_ready_and_says_why(tmp_path):
    out, lines = run_task("SELECT 1 AS n", files(tmp_path, TOP, credentials=None), language="sql")
    assert out == [{"n": 1}]
    assert any(s == "stderr" and "s3 is not set up for this query" in m and "first lease" in m for s, m in lines.out)
