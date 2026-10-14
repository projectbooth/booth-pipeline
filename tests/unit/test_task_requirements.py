"""ADR 0115: a task's own Python packages (design: docs/design-task-requirements.md).

What a real cluster has to prove (the installer reaches only the index, holds no credentials, and
real pip against a real index) is in tests/integration/pip.sh. These pin the parts that need no
cluster: what a task may list, the installer's own behaviour around pip, how a task's Job is
composed, and how failures reach the task's log.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from booth_pipeline import pip_install
from booth_pipeline.model import MAX_REQUIREMENTS, ModelError, TaskConfig, validate_task_config
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskFailed, TaskInvocation
from booth_pipeline.runners.kubejob import NO_PIP_ROUTE, KubernetesJobRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .test_kubejob import FakeCluster, Lines, make_runner, template

# ---- what a task may list (validated at save) ---------------------------------------------------


def config(reqs: list[str], source: str = "print(1)\n", language: str | None = None) -> TaskConfig:
    code = {"type": "inline", "source": source} if language is None else {"type": "catalog", "entryId": "e", "version": "1", "language": language, "source": source}
    return TaskConfig.model_validate({"code": code, "requirements": reqs})


@pytest.mark.parametrize(
    "reqs",
    [
        [],
        ["requests"],
        ["requests==2.32.3", "beautifulsoup4>=4.12,<5", "pandas[performance]~=2.2"],
        ['tomli>=2; python_version < "3.11"'],
        ["somepkg==2.0b1"],  # an exactly pinned pre-release: pip installs it only because it is named
        ["Requests", "urllib3"],
    ],
)
def test_package_specifiers_are_accepted(reqs):
    validate_task_config(config(reqs))


def test_entries_are_trimmed():
    assert config(["  requests==2.32.3  "]).requirements == ["requests==2.32.3"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("--index-url https://evil.example/simple", "pip options are not allowed"),
        ("--extra-index-url=https://evil.example/simple", "pip options are not allowed"),
        ("-r other.txt", "pip options are not allowed"),
        ("-e .", "pip options are not allowed"),
        ("--pre", "pip options are not allowed"),
        ("requests --hash=sha256:abc", "not a valid requirement"),
        ("requests @ https://evil.example/requests.whl", "URLs and paths are not allowed"),
        ("requests@file:///tmp/x.whl", "URLs and paths are not allowed"),
        ("https://evil.example/pkg.tar.gz", "URLs and paths are not allowed"),
        ("git+https://github.com/x/y", "URLs and paths are not allowed"),
        ("./local-package", "URLs and paths are not allowed"),
        ("/abs/path/pkg.whl", "URLs and paths are not allowed"),
        ("requests\n--index-url https://evil.example", "must be one line"),
        ("requests # pinned", "not a valid requirement"),
        ("", "is empty"),
        ("not a package!", "not a valid requirement"),
        ("x" * 201, "limited to 200 characters"),
    ],
)
def test_anything_but_a_package_specifier_is_refused_naming_the_entry(entry, message):
    with pytest.raises(ModelError, match=message) as e:
        validate_task_config(config(["requests", entry]))
    assert e.value.field == "requirements[1]"


def test_a_name_listed_twice_is_refused_even_spelled_differently():
    with pytest.raises(ModelError, match=r"listed twice \(also requirements\[0\]\)") as e:
        validate_task_config(config(["beautifulsoup4", "BeautifulSoup4==4.12"]))
    assert e.value.field == "requirements[1]"


def test_count_and_size_are_bounded():
    with pytest.raises(ModelError, match=f"at most {MAX_REQUIREMENTS}"):
        validate_task_config(config([f"pkg{i}" for i in range(MAX_REQUIREMENTS + 1)]))
    with pytest.raises(ModelError, match="16384 bytes in all"):
        validate_task_config(config([f"p{i}" + "a" * 190 for i in range(90)]))


def test_only_a_python_task_can_install_packages():
    validate_task_config(config(["requests"], language="python"))
    with pytest.raises(ModelError, match="only a Python task") as e:
        validate_task_config(config(["requests"], source="select 1", language="sql"))
    assert e.value.field == "requirements"
    validate_task_config(config([], source="select 1", language="sql"))


def test_requirements_round_trip_on_the_wire():
    c = config(["requests==2.32.3"])
    assert c.model_dump(by_alias=True, mode="json")["requirements"] == ["requests==2.32.3"]
    assert TaskConfig.model_validate({"code": {"type": "inline", "source": "x=1"}}).requirements == []


# ---- the installer, around pip --------------------------------------------------------------------

FAKE_PIP = textwrap.dedent(
    """
    import os, sys, time
    args = sys.argv[1:]
    target = args[args.index("--target") + 1]
    reqs = open(args[args.index("-r") + 1]).read().split()
    mode = os.environ.get("FAKE_PIP_MODE") or open(os.path.join(os.path.dirname(__file__), "mode")).read()
    print("Collecting " + reqs[0], flush=True)
    if mode == "ok":
        os.makedirs(os.path.join(target, "fakepkg"))
        open(os.path.join(target, "fakepkg", "__init__.py"), "w").write("VALUE = 42\\n")
        print("Successfully installed fakepkg-1.0")
    elif mode == "missing":
        print("ERROR: Could not find a version that satisfies the requirement nosuch==1.0 (from versions: none)")
        print("ERROR: No matching distribution found for nosuch==1.0")
        sys.exit(1)
    elif mode == "unreachable":
        print("WARNING: Retrying (Retry(total=1, connect=None, read=None, redirect=None, status=None)) after connection broken by "
              "'NewConnectionError(\\"<pip._vendor.urllib3.connection.HTTPSConnection object>: Failed to establish a new connection: [Errno 111] Connection refused\\")': /simple/requests/")
        print("ERROR: Could not find a version that satisfies the requirement requests (from versions: none)")
        print("ERROR: No matching distribution found for requests")
        sys.exit(1)
    elif mode == "noisy":
        for i in range(500):
            print(f"line {i} " + "x" * 200)
        sys.exit(1)
    elif mode == "hang":
        time.sleep(60)
    elif mode == "big":
        open(os.path.join(target, "big.bin"), "wb").write(os.urandom(2 * 1024 * 1024))
    """
)


@pytest.fixture()
def fake_pip(tmp_path):
    d = tmp_path / "fakepip"
    d.mkdir()
    (d / "pip.py").write_text(FAKE_PIP, encoding="utf-8")

    def run(mode: str, target: Path | None = None, **kw):
        (d / "mode").write_text(mode, encoding="utf-8")
        kw.setdefault("timeout_seconds", 30)
        kw.setdefault("size_limit", 1024**3)
        return pip_install.install(["requests"], target or tmp_path / "site", pip_command=[sys.executable, str(d / "pip.py")], out=open(os.devnull, "w"), **kw)  # noqa: SIM115

    return run


def test_a_successful_install_leaves_the_packages_its_log_and_a_record(fake_pip, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "left-over-from-a-previous-attempt").write_text("x")
    record = fake_pip("ok")
    assert (site / "fakepkg" / "__init__.py").exists()
    assert not (site / "left-over-from-a-previous-attempt").exists()
    assert not (site / pip_install.WORK_DIR).exists()  # pip's HOME and TMPDIR are gone
    assert "Successfully installed fakepkg-1.0" in (site / pip_install.LOG_FILE).read_text()
    assert json.loads((site / pip_install.RECORD_FILE).read_text()) == record
    assert record["packages"] == 1 and record["bytes"] > 0


def test_a_package_that_does_not_exist_is_not_blamed_on_the_network(fake_pip):
    with pytest.raises(pip_install.InstallFailed) as e:
        fake_pip("missing")
    assert str(e.value).startswith("pip install failed:\n")
    assert "No matching distribution found for nosuch==1.0" in str(e.value)
    assert "could not reach" not in str(e.value)


def test_an_unreachable_index_says_so_naming_the_host(fake_pip):
    with pytest.raises(pip_install.InstallFailed, match=r"^pip install could not reach the package index at pypi\.internal:\n"):
        fake_pip("unreachable", index_url="http://pypi.internal:8080/simple")
    with pytest.raises(pip_install.InstallFailed, match=r"at pypi\.org:"):
        fake_pip("unreachable")  # no indexUrl: pip's default


def test_the_failure_message_fits_a_termination_message(fake_pip):
    with pytest.raises(pip_install.InstallFailed) as e:
        fake_pip("noisy")
    msg = str(e.value)
    assert len(msg.encode()) < 4096 - 200
    assert msg.splitlines()[-1].startswith("line 499 ")  # the newest lines are the ones kept
    assert len(msg.splitlines()) <= pip_install.MESSAGE_LINES + 1


def test_a_hung_install_is_killed_at_the_timeout(fake_pip):
    started = time.monotonic()
    with pytest.raises(pip_install.InstallFailed, match=r"^pip install timed out after 1s \(runner\.pip\.timeoutSeconds\)$"):
        fake_pip("hang", timeout_seconds=1)
    assert time.monotonic() - started < 10


def test_an_install_over_the_size_limit_fails_rather_than_waiting_for_an_eviction(fake_pip):
    with pytest.raises(pip_install.InstallFailed, match=r"^pip install used 2\.\d MiB, more than runner\.pip\.sizeLimit \(1\.0 MiB\)$"):
        fake_pip("big", size_limit=1024 * 1024)


def test_main_writes_the_reason_as_the_termination_message_and_exits_1(tmp_path, monkeypatch):
    d = tmp_path / "fakepip"
    d.mkdir()
    (d / "pip.py").write_text(FAKE_PIP, encoding="utf-8")
    (d / "mode").write_text("missing", encoding="utf-8")
    term = tmp_path / "termination-log"
    monkeypatch.setattr(pip_install, "PIP_COMMAND", [sys.executable, str(d / "pip.py")])
    monkeypatch.setenv("BOOTH_PIP_REQUIREMENTS", "nosuch==1.0")
    monkeypatch.setenv("BOOTH_PIP_TARGET", str(tmp_path / "site"))
    monkeypatch.setenv("BOOTH_PIP_TERMINATION_LOG", str(term))
    assert pip_install.main() == 1
    assert term.read_text().startswith("pip install failed:\n")
    assert "No matching distribution found for nosuch==1.0" in term.read_text()


@pytest.mark.parametrize(("q", "n"), [("1Gi", 1024**3), ("512Mi", 512 * 1024**2), ("1G", 10**9), ("1048576", 1048576), ("1.5Gi", int(1.5 * 1024**3))])
def test_storage_quantities(q, n):
    assert pip_install.parse_quantity(q) == n


# ---- the task's process finds them; a task never runs without them ---------------------------------


def installed_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    (site / "fakepkg").mkdir(parents=True)
    (site / "fakepkg" / "__init__.py").write_text("VALUE = 42\n")
    (site / pip_install.LOG_FILE).write_text("Collecting fakepkg\nSuccessfully installed fakepkg-1.0\n")
    (site / pip_install.RECORD_FILE).write_text(json.dumps({"packages": 1, "seconds": 2.5, "bytes": 3 * 1024 * 1024}))
    return site


def task(source: str, reqs: tuple[str, ...] = ("fakepkg",), access: TaskAccess | None = None) -> TaskInvocation:
    return TaskInvocation("run-1", "t", "", 1, source, {}, {}, 60, access, requirements=reqs)


def test_the_task_imports_its_packages_and_its_log_shows_the_install(tmp_path):
    site = installed_site(tmp_path)
    lines = Lines()
    src = "import fakepkg, os\ndef run(ctx):\n    print('value', fakepkg.VALUE, os.environ['PYTHONPATH'])\n    return fakepkg.VALUE\n"
    assert SubprocessRunner(site_dir=str(site)).run(task(src), lines, Cancellation()) == 42
    assert lines.stdout == [f"value 42 {site}"]
    system = [m for s, m in lines.out if s == "system"]
    assert system == ["pip: Collecting fakepkg", "pip: Successfully installed fakepkg-1.0", "packages installed in 2.5s (3.0 MiB)"]


def test_the_tasks_packages_come_before_the_images_own(tmp_path):
    site = installed_site(tmp_path)
    (site / "yaml").mkdir()
    (site / "yaml" / "__init__.py").write_text("__version__ = 'the task asked for this one'\n")
    src = "import yaml\ndef run(ctx):\n    return yaml.__version__\n"
    assert SubprocessRunner(site_dir=str(site)).run(task(src, ("pyyaml",)), Lines(), Cancellation()) == "the task asked for this one"


def test_a_task_without_requirements_sees_no_site(tmp_path):
    site = installed_site(tmp_path)
    src = "import os\ndef run(ctx):\n    return [os.environ.get('PYTHONPATH'), os.environ.get('BOOTH_SITE_DIR')]\n"
    assert SubprocessRunner(site_dir=str(site)).run(task(src, ()), Lines(), Cancellation()) == [None, None]


def test_a_task_whose_packages_were_not_installed_never_runs(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    marker = tmp_path / "ran"
    src = f"open({str(marker)!r}, 'w').write('x')\n"
    with pytest.raises(TaskFailed, match="packages were not installed"):
        SubprocessRunner(site_dir=str(empty)).run(task(src), Lines(), Cancellation())
    with pytest.raises(TaskFailed, match="only installed for tasks run as Kubernetes Jobs"):
        SubprocessRunner().run(task(src), Lines(), Cancellation())
    assert not marker.exists()


# ---- the task's Job ----------------------------------------------------------------------------------


def pip_template(sidecar: bool = False) -> dict:
    """What the chart renders (charts/booth-pipeline/templates/_helpers.tpl, booth-pipeline.taskJob)."""
    t = template()
    p = t["spec"]["template"]["spec"]
    p["initContainers"] = [
        {
            "name": "pip",
            "image": "booth-pipeline:test",
            "command": ["booth-pipeline-pip"],
            "env": [{"name": "BOOTH_PIP_TARGET", "value": "/opt/booth/site"}, {"name": "BOOTH_PIP_TIMEOUT_SECONDS", "value": "120"}],
            "volumeMounts": [{"name": "site", "mountPath": "/opt/booth/site"}],
        }
    ]
    if sidecar:
        p["initContainers"].append(
            {
                "name": "credential-sidecar-postgres",
                "image": "ghcr.io/projectbooth/credential-sidecar@sha256:" + "a" * 64,
                "restartPolicy": "Always",
                "args": ["--kind=postgres", "--listen=127.0.0.1:5432", "--token-file=/var/run/booth-sidecar/token", "--core-url=http://core"],
                "volumeMounts": [{"name": "booth-sidecar", "mountPath": "/var/run/booth-sidecar", "readOnly": True}],
            }
        )
        p["volumes"].append({"name": "booth-sidecar", "emptyDir": {"medium": "Memory"}})
    task_c = p["containers"][0]
    task_c["env"] = [{"name": "BOOTH_RUNNER_SITE_DIR", "value": "/opt/booth/site"}]
    task_c["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}, {"name": "site", "mountPath": "/opt/booth/site", "readOnly": True}]
    p["volumes"].append({"name": "site", "emptyDir": {"sizeLimit": "1Gi"}})
    return t


def job_for(reqs: tuple[str, ...], sidecar: bool = False, access: TaskAccess | None = None) -> dict:
    r = KubernetesJobRunner(None, pip_template(sidecar), pip_route="index")  # type: ignore[arg-type]
    return r._job("booth-pipeline-task-abc", task("x", reqs, access))


def test_a_task_without_requirements_gets_no_trace_of_pip():
    job = job_for(())
    assert '"pip"' not in json.dumps(job) and '"site"' not in json.dumps(job) and "BOOTH_RUNNER_SITE_DIR" not in json.dumps(job)
    assert "initContainers" not in job["spec"]["template"]["spec"]
    # Exactly the Job of the same template written without any pip pieces.
    plain = pip_template()
    p = plain["spec"]["template"]["spec"]
    del p["initContainers"]
    p["volumes"] = [v for v in p["volumes"] if v["name"] != "site"]
    p["containers"][0]["env"] = []
    p["containers"][0]["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}]
    assert job == KubernetesJobRunner(None, plain)._job("booth-pipeline-task-abc", task("x", ()))  # type: ignore[arg-type]


def test_a_task_with_requirements_gets_them_in_a_first_credential_less_init_container():
    access = TaskAccess("ws", "platform-token-that-must-never-reach-a-kubernetes-object", "s", "c")
    job = job_for(("requests==2.32.3", "beautifulsoup4"), sidecar=True, access=access)
    p = job["spec"]["template"]["spec"]
    pip, sidecar = p["initContainers"]
    assert pip["name"] == "pip" and sidecar["name"] == "credential-sidecar-postgres"  # pip finishes before any sidecar starts
    assert {"name": "BOOTH_PIP_REQUIREMENTS", "value": "requests==2.32.3\nbeautifulsoup4"} in pip["env"]
    assert [m["name"] for m in pip["volumeMounts"]] == ["site"]
    assert "platform-token" not in json.dumps(job)
    (site_mount,) = [m for m in p["containers"][0]["volumeMounts"] if m["name"] == "site"]
    assert site_mount["readOnly"] is True
    assert job["spec"]["activeDeadlineSeconds"] == job_for(())["spec"]["activeDeadlineSeconds"] + 120


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["initContainers"].reverse(), "must be the first init container"),
        (lambda p: p["initContainers"][0]["volumeMounts"].append({"name": "runner-auth", "mountPath": "/x"}), "must mount only the 'site' volume"),
        (lambda p: p["initContainers"][0].update(envFrom=[{"secretRef": {"name": "s"}}]), "must not take environment from Secrets"),
        (lambda p: p["initContainers"][0].update(restartPolicy="Always"), "not as a sidecar"),
        (lambda p: p["initContainers"][0].update(env=[]), "needs BOOTH_PIP_TIMEOUT_SECONDS"),
    ],
)
def test_a_template_whose_installer_could_see_more_than_its_volume_is_refused(mutate, message):
    t = pip_template(sidecar=True)
    mutate(t["spec"]["template"]["spec"])
    with pytest.raises(ValueError, match=message):
        KubernetesJobRunner(None, t)  # type: ignore[arg-type]


@pytest.fixture()
def cluster():
    c = FakeCluster()
    yield c
    c.close()


def test_with_no_route_to_an_index_the_task_fails_at_once_naming_the_value_to_set(cluster):
    r = make_runner(cluster, pip_template(), pip_route="")
    with pytest.raises(TaskFailed) as e:
        r.run(task("print(1)\n"), Lines(), Cancellation())
    assert str(e.value) == NO_PIP_ROUTE
    assert "runner.pip.egress" in NO_PIP_ROUTE and "allowInternet=true" in NO_PIP_ROUTE
    assert not cluster.requests  # no Job was ever created


def test_a_failed_install_fails_the_task_with_the_installers_reason(cluster):
    cluster.pod_status = {
        "phase": "Failed",
        "initContainerStatuses": [
            {"name": "pip", "state": {"terminated": {"exitCode": 1, "reason": "Error", "message": "pip install failed:\nERROR: No matching distribution found for nosuch==1.0"}}}
        ],
    }
    lines = Lines()
    with pytest.raises(TaskFailed) as e:
        make_runner(cluster, pip_template(), pip_route="index").run(task("print(1)\n", ("nosuch==1.0",)), lines, Cancellation())
    assert str(e.value) == "pip install failed:\nERROR: No matching distribution found for nosuch==1.0"
    assert ("system", "installing 1 Python requirement(s) before the task starts: nosuch==1.0") in lines.out
    assert len(cluster.deleted) == 1 and not cluster.jobs


def test_the_install_gets_its_own_time_on_top_of_the_start_timeout(cluster):
    cluster.pod_problem = "never-ready"
    r = make_runner(cluster, pip_template(), pip_route="internet", start_timeout_seconds=1)
    r._pip_timeout = 1
    with pytest.raises(TaskFailed, match="did not become ready within 2s"):
        r.run(task("print(1)\n"), Lines(), Cancellation())


# ---- the API: saving, exporting, and whether this install can install at all -------------------------


@pytest.fixture()
def env():
    from .harness import make_env

    e = make_env()
    with e.client:
        yield e


def test_a_task_saves_its_requirements_and_a_bad_entry_is_a_422_naming_it(env):
    code = {"type": "inline", "source": "import requests\n"}
    r = env.call("POST", "/tasks", json={"name": "scrape", "config": {"code": code, "requirements": ["requests==2.32.3", " beautifulsoup4 "]}})
    assert r.status_code == 201, r.text
    t = env.call("GET", f"/tasks/{r.json()['id']}/versions/1").json()
    assert t["config"]["requirements"] == ["requests==2.32.3", "beautifulsoup4"]
    r = env.call("POST", "/tasks", json={"name": "bad", "config": {"code": code, "requirements": ["requests", "x @ https://evil.example/x.whl"]}})
    assert r.status_code == 422 and r.json()["field"] == "requirements[1]" and "URLs and paths are not allowed" in r.json()["error"]


def test_the_export_carries_each_tasks_requirements(env):
    import yaml

    from .harness import task as node

    spec = {"tasks": [node("scrape", requirements=["requests==2.32.3", "beautifulsoup4"])]}
    p = env.call("POST", "/pipelines", json={"name": "nfl", "spec": spec}).json()
    doc = yaml.safe_load(env.call("GET", f"/pipelines/{p['id']}/versions/1/export").text)
    assert doc["spec"]["tasks"][0]["task"]["config"]["requirements"] == ["requests==2.32.3", "beautifulsoup4"]


def test_config_says_local_development_cannot_install_packages(env):
    pip = env.call("GET", "/config").json()["pip"]
    assert pip["available"] is False and "Kubernetes Jobs" in pip["reason"]


@pytest.mark.parametrize(
    ("route", "available"),
    [("index", True), ("internet", True), ("extra", True), ("unrestricted", True), ("", False), ("nonsense", False)],
)
def test_config_reports_the_route_or_the_value_to_set(route, available):
    from booth_pipeline.api import pip_status
    from booth_pipeline.config import Config

    status = pip_status(Config(task_job_template="/etc/booth/task-job/job.json", pip_route=route))
    assert status["available"] is available
    if not available:
        assert status["reason"] == NO_PIP_ROUTE
