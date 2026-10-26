"""Runner images per task: an operator-built image (runner.images, pinned by digest) a task chooses at save,
swapped into its Job's pod and pip init container, and refused at start when its runner service speaks
another revision of the runner protocol."""

from __future__ import annotations

import pytest

from booth_pipeline.config import Config, ConfigError
from booth_pipeline.model import ModelError, TaskConfig, validate_task_config
from booth_pipeline.runners.base import RUNNER_PROTOCOL, Cancellation, TaskAccess, TaskFailed, TaskInvocation
from booth_pipeline.runners.kubejob import KubernetesJobRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .harness import make_env
from .test_kubejob import FakeCluster, Lines, make_runner
from .test_task_requirements import pip_template

STOCKS = "ghcr.io/you/stocks-runner@sha256:" + "b" * 64
IMAGES = {"stocks-py": STOCKS}


def inv(source: str = "def run(ctx):\n    return 'ran'\n", image: str = "", reqs: tuple[str, ...] = (), access: TaskAccess | None = None) -> TaskInvocation:
    return TaskInvocation("run-1", "t", "", 1, source, {}, {}, 60, access=access, requirements=reqs, runner_image=image)


@pytest.fixture()
def cluster():
    c = FakeCluster()
    yield c
    c.close()


# ---- the Job ------------------------------------------------------------------------------------


def test_the_task_and_its_pip_container_run_the_chosen_image_and_sidecars_keep_theirs():
    r = KubernetesJobRunner(None, pip_template(sidecar=True), pip_route="index", images=IMAGES)  # type: ignore[arg-type]
    access = TaskAccess("acme", "tok", "http://x", "http://x")  # a credential sidecar stays only for a task with access
    pod = r._job("n", inv(image="stocks-py", reqs=("ta-lib==0.4.32",), access=access))["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == STOCKS
    pip, *sidecars = pod["initContainers"]
    assert pip["name"] == "pip" and pip["image"] == STOCKS  # requirements install on top, with the image's own Python
    assert sidecars and all(c["image"] != STOCKS for c in sidecars)


def test_a_task_without_one_keeps_the_modules_image():
    r = KubernetesJobRunner(None, pip_template(), pip_route="index", images=IMAGES)  # type: ignore[arg-type]
    pod = r._job("n", inv())["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] != STOCKS


def test_a_task_on_a_current_image_runs_and_says_which_image(cluster):
    lines = Lines()
    assert make_runner(cluster, images=IMAGES).run(inv(image="stocks-py"), lines, Cancellation()) == "ran"
    assert ("system", f"runner image stocks-py: {STOCKS}") in lines.out


@pytest.mark.parametrize(
    ("protocol", "message"),
    [
        (None, "is older than this booth-pipeline (its runner service doesn't report its protocol"),
        (RUNNER_PROTOCOL - 1, "is older than this booth-pipeline (built from"),
        (RUNNER_PROTOCOL + 1, "is newer than this booth-pipeline (built from"),
    ],
)
def test_an_image_from_another_release_is_refused_before_the_task_runs(cluster, protocol, message):
    cluster.runner_protocol = protocol
    src = "def run(ctx):\n    open('/tmp/booth-should-not-exist', 'w')\n"
    with pytest.raises(TaskFailed) as e:
        make_runner(cluster, images=IMAGES).run(inv(src, image="stocks-py"), Lines(), Cancellation())
    assert message in str(e.value) and "'stocks-py'" in str(e.value)
    assert "rebuild it FROM this release's booth-pipeline image" in str(e.value)
    assert len(cluster.deleted) == 1  # the Job is still cleaned up


def test_the_modules_own_image_is_not_asked(cluster):
    cluster.runner_protocol = None  # it would fail the check; the module's own image is never asked
    assert make_runner(cluster, images=IMAGES).run(inv(), Lines(), Cancellation()) == "ran"


def test_an_image_removed_from_the_chart_after_save_fails_the_task_before_any_job(cluster):
    with pytest.raises(TaskFailed, match="runner image 'gone' is not configured on this install any more"):
        make_runner(cluster, images=IMAGES).run(inv(image="gone"), Lines(), Cancellation())
    assert not cluster.jobs and not cluster.deleted


def test_in_process_tasks_refuse_a_runner_image():
    with pytest.raises(TaskFailed, match="only a task run as a Kubernetes Job can use"):
        SubprocessRunner().run(inv(image="stocks-py"), Lines(), Cancellation())


# ---- the model and the configuration --------------------------------------------------------------


def cfg(**kw) -> TaskConfig:
    return TaskConfig.model_validate({"code": {"type": "inline", "source": "pass\n"}, **kw})


def test_a_task_chooses_one_of_the_configured_images():
    validate_task_config(cfg(runnerImage="stocks-py"), runner_images={"stocks-py"})
    validate_task_config(cfg(runnerImage="stocks-py", requirements=["ta-lib"]), runner_images={"stocks-py"})  # packages on top
    with pytest.raises(ModelError, match="available: stocks-py") as e:
        validate_task_config(cfg(runnerImage="other"), runner_images={"stocks-py"})
    assert e.value.field == "runnerImage"
    with pytest.raises(ModelError, match="has none"):
        validate_task_config(cfg(runnerImage="stocks-py"), runner_images=frozenset())
    with pytest.raises(ModelError, match="Spark"):
        validate_task_config(cfg(runner="spark", spark={}, runnerImage="stocks-py"), runner_images={"stocks-py"})


def base_cfg(**kw) -> Config:
    return Config(dev_memory=True, oidc_issuer_url="https://idp/x", oidc_require_audience=False, **kw)


def test_configured_images_must_be_pinned_by_digest_and_well_named():
    base_cfg(runner_images=IMAGES).validate()
    with pytest.raises(ConfigError, match="pinned by digest"):
        base_cfg(runner_images={"stocks-py": "ghcr.io/you/stocks-runner:latest"}).validate()
    with pytest.raises(ConfigError, match="not a usable name"):
        base_cfg(runner_images={"Stocks": STOCKS}).validate()


def test_the_images_come_from_the_environment_as_json(monkeypatch):
    monkeypatch.setenv("BOOTH_OIDC_ISSUER_URL", "https://idp/x")
    monkeypatch.setenv("BOOTH_OIDC_REQUIRE_AUDIENCE", "false")
    monkeypatch.setenv("BOOTH_PIPELINE_DEV_MEMORY", "true")
    monkeypatch.setenv("BOOTH_PIPELINE_RUNNER_IMAGES", f'{{"stocks-py": "{STOCKS}"}}')
    assert Config.from_env().runner_images == IMAGES
    monkeypatch.setenv("BOOTH_PIPELINE_RUNNER_IMAGES", "[1]")
    with pytest.raises(ConfigError, match="JSON object"):
        Config.from_env()


# ---- the API ------------------------------------------------------------------------------------------


def test_the_builder_is_told_the_names_and_a_task_is_checked_against_them_at_save():
    env = make_env(runner_images=IMAGES)
    try:
        assert env.call("GET", "/config").json()["runnerImages"] == ["stocks-py"]
        ok = env.call("POST", "/tasks", json={"name": "capture", "config": {"code": {"type": "inline", "source": "pass\n"}, "runnerImage": "stocks-py"}})
        assert ok.status_code == 201, ok.text
        t = ok.json()["id"]
        assert env.call("GET", f"/tasks/{t}/versions/1").json()["config"]["runnerImage"] == "stocks-py"
        bad = env.call("POST", "/tasks", json={"name": "other", "config": {"code": {"type": "inline", "source": "pass\n"}, "runnerImage": "nope"}})
        assert bad.status_code == 422 and bad.json()["field"] == "runnerImage"
    finally:
        env.client.close()
