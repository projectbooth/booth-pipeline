# Design: a task's own Python packages (ADR 0115)

Status: **accepted and built** (ADR 0115 amendment 1 adopted section 9's ruling, 2026-10-13). Section 11 lists
where the build differs from the proposal below. Inputs: booth-architecture
`decisions/0115-pipeline-tasks-install-their-own-python-packages.md`; booth-streamlit's
`apps.pip` (`docs/design-data-access.md`, section 5 and "pip install" under "As built"), the
precedent copied here wherever the two modules are alike; this repo's ADR 0096 design
(`docs/decisions/0018`) and its task Job template (`charts/booth-pipeline/templates/_helpers.tpl`,
`booth-pipeline.taskJob`).

Section 9 has one point where ADR 0115 Decision 4 can't hold as written, with a recommended
ruling. Nothing is built until that, and this note, are agreed.

## 1. Model

**Where requirements live: on the task version** (`TaskConfig`, ADR 0071), as a new field
`requirements: list[str]` (default `[]`), next to `code`, `params` and `timeoutSeconds`. That
is the ADR's default ("per task, so tasks stay standalone units"). It is versioned and
snapshotted exactly like the code: saving a task version freezes both together, and a pipeline
version reaches them through its task references the same way it reaches the code. A pinned
reference gets that version's packages. An "Always follow latest" reference (ADR 0073) picks up
the task's current packages at run time, as it already does for the code. Per pipeline or per
pipeline-version requirements are not added: two tasks in one pipeline may need conflicting
versions, and each task already runs in its own pod.

**YAML export** (`export.py`) needs no new code. `task.config` is the whole `TaskConfig`, so
each task gains `requirements:` as a list of strings, one per line, beside `code`. The future
import reads the same field.

**Validation at save** (`validate_task_config`, pure, the same at task save and at validation):

- **Language:** only a Python task (`code_language(code) == "python"`) may have requirements. A SQL
  task with any is refused (`requirements: only a Python task can install packages`).
- **Format:** each entry is one PEP 508 requirement, parsed with `packaging.requirements.Requirement`
  (`packaging` becomes an explicit dependency; today it only comes in through dagster).
  - **Allowed:** a name, extras, version specifiers (`==`, `>=`, `~=`, `<`, `!=`, ranges) and
    environment markers.
  - **Refused, with the entry's index in the error (`requirements[3]`):**
    - anything starting with `-`: `--index-url`, `--extra-index-url`, `-r`, `-e`, `--pre`,
      `--hash`, any pip option;
    - direct references (`name @ https://…`, `name @ file://…`);
    - bare URLs, paths and VCS (`git+…`);
    - blank and comment-only entries (the builder strips them before sending);
    - an entry pip would read as a file or option;
    - a second entry naming the same normalized project name.
- **No URLs, index packages only.** The index is the operator's (`runner.pip.indexUrl`), never
  the task's. A task can't name its own index or download location.
- **Pre-releases:** no `--pre`. An entry that names a pre-release exactly (`==2.0b1`) is allowed,
  because pip only installs a pre-release when the specifier names one. Allowing it costs nothing
  and blocks no one.
- **Size:** at most 100 entries, each at most 200 characters, and at most 16 KiB in all (as
  Streamlit). Names, versions and markers are short; these limits only catch pasted junk.
- **Not checked at save:** whether a package or version exists on the index. Save is pure, with no
  network access, and the index a run uses is decided at run time. A requirement that can't be met
  fails the run clearly (section 5).

## 2. The init container in a one-Job-per-task pod

A task with requirements gets one extra container in its Job, `pip`. A task without requirements
gets exactly today's Job spec: no init container, no new volume and no new environment variable
(a contract test pins this).

### Container and command

- **Image:** the task image itself, the same `python:3.13` interpreter that runs the task, so the
  wheels match it.
- **Command:** a new entrypoint, `booth-pipeline-pip` (`booth_pipeline.pip_install`, standard library
  only).
- **Runs as:** uid 65532 with the pod's securityContext and the container securityContext already
  used (read-only root filesystem, no privilege escalation, all capabilities dropped).

### Order

`pip` is the **first** entry in `initContainers`. The credential sidecars are native sidecars
(`restartPolicy: Always`) later in the same list. Kubernetes starts init containers in order and
starts a native sidecar only after every init container before it has completed. So while `pip`
runs:

- no sidecar process exists;
- nothing listens on 127.0.0.1:5432 or :8081;
- the task container has not started.

### Volumes

- **`site`:** a disk `emptyDir` with `sizeLimit: runner.pip.sizeLimit` (default 1Gi).
  - Mounted read-write in `pip` at `/opt/booth/site`.
  - Mounted **read-only** in the task container at the same path.
  - pip's HOME and TMPDIR go to `/opt/booth/site/.work`, which the installer deletes afterwards.
  - The task's own `/tmp` scratch volume is not shared with `pip`.
- **`pip` mounts nothing else.** It has:
  - no `runner-auth` Secret;
  - no `booth-sidecar` token volume;
  - no `booth-sidecar-s3`;
  - no ServiceAccount token (`automountServiceAccountToken: false` is already pod-wide).

### Environment

`pip` gets only these variables:

- `PIP_INDEX_URL`, plus `PIP_TRUSTED_HOST` if set;
- the timeout and size limit;
- `BOOTH_PIP_REQUIREMENTS`: the validated requirements joined with newlines, written into the Job
  spec by `KubernetesJobRunner._job`.

It gets no `BOOTH_*` runner settings, no `AWS_*`, no `DATABASE_URL`. `enableServiceLinks: false` is
already pod-wide.

Requirements aren't secret, and 16 KiB is far below the object size limit. That is why they go in
the spec, not in the per-task Secret: the Secret holds the runner bearer, which `pip` must never
mount.

### Why the task's credentials can't reach `pip`

Its platform token reaches the pod only after the pod is Ready. `KubernetesJobRunner` sends the
token in the POST it makes to the runner service, which starts in the task container. That
container cannot start until `pip` has finished, so the token does not exist anywhere in the pod
while untrusted install code, including an sdist's `setup.py`, runs. This is ADR 0115 Decision 3,
and it holds by the pod's own start order, not by a convention. Section 7 proves it on a real
cluster.

### How the task finds the packages

- **Only the task's subprocess sees them.** The runner service sets `PYTHONPATH=/opt/booth/site`
  in `SubprocessRunner._env`, which builds the task subprocess's environment from scratch. The
  container itself does not get the variable.
- **The runner can't be broken by them.** The runner service never puts `/opt/booth/site` on its
  own path, so a task that pins an old `httpx` can't break it.
- **The harness is unaffected.** It uses only the standard library, so a task's packages can't
  shadow anything it needs.
- **The task's own versions win.** `PYTHONPATH` comes before the image's site-packages, so the
  versions the task asked for are the ones it imports. `pip --target` installs every dependency
  into the target, including ones the image already has.
- **The root filesystem stays read-only.** Nothing is written outside the `emptyDir` volumes.

### What the installer does

1. It empties `/opt/booth/site`. A rerun finds the previous attempt's files; with `restartPolicy:
   Never` that can't happen, but it is cheap insurance.
2. It writes the requirements to `.work/requirements.txt`.
3. It runs:

   ```
   pip install --no-cache-dir --target /opt/booth/site --prefer-binary \
     --disable-pip-version-check --no-input --timeout 20 --retries 2 -r .work/requirements.txt
   ```

   - The output goes to the console and to `/opt/booth/site/.booth-pip.log`.
   - The whole install is killed at `runner.pip.timeout`. pip's own `--timeout` applies to each
     network read, so a server that trickles bytes never trips it (Streamlit measured this).
4. It checks how much the install used. Kubernetes enforces an `emptyDir` sizeLimit by evicting
   the pod some time later, so over the limit the installer fails at once with a clear message
   instead.
5. On failure it writes the tail of pip's output as its termination message (at most 20 lines and
   2,500 bytes, under Kubernetes' 4 KiB limit) and exits 1.

### Building from source

`--prefer-binary`, not `--only-binary`, as ADR 0115 Decision 5 says. An sdist is built in `pip`,
which holds no credentials (Decision 6). The image has no C compiler, so an sdist that needs one
fails with pip's own message.

### Timing

- **Pod readiness:** the API pod waits up to `startTimeoutSeconds` for the pod to become Ready. A
  task with requirements gets `runner.pip.timeout` on top, and the Job's `activeDeadlineSeconds`
  grows by the same amount.
- **What install time counts against:** it is start time. It does not count against the task's own
  `timeoutSeconds`.

## 3. Chart values

```yaml
runner:
  pip:
    # The package index (PIP_INDEX_URL). Empty: pip's default, PyPI.
    indexUrl: ""
    # Only for an HTTP index you control (e.g. an in-cluster mirror). It gives up transport
    # integrity for that host. Leave empty for HTTPS.
    trustedHost: ""
    # The whole install's limit. pip's own --timeout covers one network read and can't catch a
    # slow trickle.
    timeout: 5m
    # Room for a task's installed packages (the site emptyDir's sizeLimit).
    sizeLimit: 1Gi
    # An in-cluster package index: its selectors and port add ONE egress rule for task pods. Both
    # selectors are required (the chart fails on only one, as Streamlit's does). NetworkPolicy is
    # per pod, so task code can reach this index too.
    egress:
      namespaceSelector: {}
      podSelector: {}
      port: 8080
```

### How these interact with `allowInternet`

This depends on the ruling in section 9. The recommendation is that a task's packages come from:

- **An in-cluster index**, set by `runner.pip.egress` selectors with `indexUrl` pointing at it.
  This works with `allowInternet=false`: the pod gets exactly one extra destination. This is the
  only setup where "the installer reaches the index and nothing else" holds.
- **Public PyPI (`indexUrl` empty) or any other external index:** this needs
  `runner.networkPolicy.egress.allowInternet=true`. The installer can then reach the internet
  except the private ranges (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16), the same as the
  task code already can.
- **An external index on a private address** (e.g. a homelab mirror on 192.168.x): neither path
  above reaches it, because the private-range exclusions stop it. Such an index needs
  `runner.networkPolicy.egress.extra`, as any other private destination does today.

### When nothing can be installed

If `allowInternet` is off and `runner.pip.egress` has no selectors, the API reports
`pip.available: false` on `GET /api/config`, with the reason. A task with requirements then:

- **still saves:** exports and other installs differ, and the chart can change;
- **fails at once when it runs,** before any Job is created, with: `this install has no route to a
  package index: set runner.pip.egress for an in-cluster index, or
  runner.networkPolicy.egress.allowInternet for PyPI`. That is faster and clearer than three
  20-second pip connect timeouts.

### Storage limits

- `runner.resources` has no default ephemeral-storage limit, so nothing has to grow.
- An operator who sets an ephemeral-storage limit must allow `sizeLimit` for the packages. The
  values comment says so.

## 4. Network policy

The task NetworkPolicy gets one extra rule, rendered only when `runner.pip.egress.podSelector` is
set:

- `to: namespaceSelector + podSelector`, on TCP `runner.pip.egress.port`.
- It applies to every task pod, with or without requirements. NetworkPolicy can't tell an init
  container from the main container, or a task that has requirements from one that doesn't, unless
  pods get different labels. That is possible, but the gain is small: a package index is a read-only
  public mirror. The values comment and the README state this.

## 5. Failure and the task log

**Success.** The task log shows:

- before the task code runs:
  - `system: installing 3 Python packages`
  - pip's output, read from `/opt/booth/site/.booth-pip.log` and streamed into the task log before
    the code runs;
- then a closing line, `system: packages installed in 8.4s (41 MiB)`.

The installer writes that log file into `site`, which the task container reads. That is why the
runner needs no new permission to read pod logs. Its Role stays get/list/watch on pods, with no
`pods/log`.

**An install failure fails the task attempt.** The `pip` container exits 1, so the pod becomes
`Failed` with `restartPolicy: Never`. `_wait_ready` already sees `Failed`. It is extended to read
`pip`'s termination message, and `TaskFailed` carries one of these:

- `pip install failed:` followed by the tail of pip's output, line breaks kept. For example
  `ERROR: No matching distribution found for nosuchpkg==1.0`, or a build error from an sdist.
- `pip install timed out after 5m0s`.
- `pip install used 1.2 GiB, more than runner.pip.sizeLimit (1Gi)`.
- `pip install could not reach the package index at <host>:` plus the tail. This is used only when
  pip's output shows a connection failure. A missing package is never blamed on the network.

The task's code never runs and the Job is deleted, as for any other failure. The engine's retry
policy applies as usual: an install that failed once is retried if the task has retries. A
requirement that doesn't exist will fail every time; that is accepted rather than special-cased.

**An eviction** for going over the size limit, which only happens if the installer's own check is
outrun, appears as the pod's `Failed` / `Evicted` reason through the existing `_why_ended` path.

## 6. Cache: not in v0

Every attempt reinstalls its packages, as Streamlit does.

**Cost.** A couple of pure-Python wheels (`requests`, `beautifulsoup4` and their dependencies,
about 1 MB) take a few seconds, on top of a pod start that already takes several. Heavy wheels
(`pandas`, `numpy`) take tens of seconds. Integration measures the first case and prints it in the
log, so the number is on record.

**A shared cache is not just a performance feature; it is a trust question.** The install step runs
untrusted code (sdist builds), so any cache it can write to lets one workspace's task poison
another's packages. A safe cache must be filled by something trusted, not by the init container.

**Where caching belongs is the index.** An operator who wants speed runs an in-cluster pull-through
mirror (devpi, proxpi, or a Nexus or Artifactory proxy) and points `runner.pip` at it. The same
setup gives the narrow, closed-by-default egress in section 3. A per-requirements-hash volume, as
the ADR suggests, is not proposed until measured install times call for it, and would need the
trusted-writer design above.

## 7. Integration proofs (Calico job, `tests/integration/pip.sh`)

### Fixture: an in-cluster index

- A PEP 503 "simple" index served by `python -m http.server` in its own namespace, `pypi-test`.
- It is built in the workflow from:
  - `requests` and `beautifulsoup4` wheels and their dependencies (downloaded by the GitHub
    runner, digest-checked against a lock file in the repo);
  - small local test packages:
    - `booth-pip-probe`: an **sdist** whose `setup.py` runs the checks below *during the build,
      inside the `pip` init container*. That is exactly the untrusted-code path Decision 6 is about.
      It writes its verdicts to `/opt/booth/site/probe.json`.
    - `booth-pip-broken`: an sdist whose build prints a marker and exits 1.
    - `booth-pip-trickle`: served by an endpoint that sends one byte every 5 seconds.
- Chart: `runner.pip.indexUrl=http://pypi.pypi-test.svc:8080/simple`, `trustedHost`, `egress`
  selectors for the fixture's pod, `timeout=30s`.
- `allowInternet` stays off throughout.

The task code in each case is a small inline task run through the real API, scheduler and Job
runner, as in `task_jobs_driver.py` today.

**Proof 1: the installer reaches only the index.** This uses `egress.sh`'s works-blocked-works
sequence, unchanged:

- The same targets as `egress.sh`: this module's PostgreSQL, the API pod, the Kubernetes API, the
  kubelet, the internet, the 10/8, 172.16/12, 192.168/16 and 100.64/10 LAN hosts, the metadata
  address, and the LAN control address. `booth-pip-probe` probes them all from `pip`.
- With the task policy deleted every target is `open`. With it applied, each is `timeout` or
  `refused`. Deleted again, `open` again.
- The index is reached in the blocked phase too, or the probe sdist could not have been
  downloaded. The probe also connects to the index's address directly and records `open`.
- The task container then probes the same targets and gets the same verdicts. That asserts
  "per pod" (section 4) is what really happens, rather than leaving it implied.

**Proof 2: no token or credentials in the installer.** This runs in the step that already has the
credential sidecar configured. The task has platform access, so its pod has the postgres sidecar
and its token. `booth-pip-probe`'s build step in `pip` asserts:

- `/var/run/secrets/kubernetes.io/serviceaccount` does not exist;
- `/proc/self/mounts` lists only `site` plus the kubelet's own files (`/etc/hosts`,
  `/etc/resolv.conf`, `/etc/hostname`, `/dev/termination-log`). It mounts no `runner-auth` and no
  `booth-sidecar` volume;
- the environment has no `BOOTH_`, `AWS_` or `DATABASE_URL` variables and nothing shaped like a
  JWT;
- 127.0.0.1:5432 and 127.0.0.1:8081 are refused, because no sidecar has started yet;
- a scan of every readable file finds neither secret: the runner bearer, which the test reads from
  the per-task Secret as cluster admin, and the task's platform token, which the test's fake core
  minted and logged. The scan is anchored by a positive control: the probe first plants a decoy
  JWT in `site`, and the scan must find it, so a broken scanner fails instead of passing.

Then the task container checks the same pod has its credentials: the token file exists and
`DATABASE_URL` connects through the sidecar. The checks above prove separation, not absence of
credentials everywhere.

**Proof 3: a failed install fails the task clearly.** For `booth-pip-broken`:

- the run's task is `failed`;
- its log has `pip install failed:` and the sdist's marker line;
- the task code's own marker is absent (it never ran);
- the Job is gone afterwards.

The trickle package also fails as `pip install timed out after 30s`, inside `timeout + 15s`.

**Proof 4: a requirement that isn't available fails clearly.**

- `booth-no-such-package==1.0`: the log has `No matching distribution found for
  booth-no-such-package==1.0`, and the message does **not** say "could not reach the package
  index".
- `requests==0.0.1`, a version that doesn't exist: the same treatment.

**Plus the success case:**

- A task requiring `requests` and `beautifulsoup4` imports both and parses a page from the fixture.
- Its log shows the install lines and `packages installed in …`, with the timing printed.

### Unit and contract tests

- Validation: every refused form in section 1, the size limits, the SQL-task refusal and
  duplicate names.
- Job composition:
  - `pip` comes first, before the sidecars;
  - its mounts, environment and no secrets;
  - `site` is read-only in the task container;
  - the deadlines grow by the pip timeout;
  - a task without requirements gets a byte-identical Job;
  - the "no route to an index" fast failure.
- The installer: termination-message trimming, the timeout kill against a fake pip, the size
  check, and how a connection failure is told apart from a missing package.
- The chart: the egress rule renders only with selectors, and the chart fails on only one
  selector.
- The export includes `requirements`.

## 8. The builder (pipeline-ui)

- **Where packages are edited:** a "Python packages" field in the task's settings (`TaskConfigForm`,
  beside timeout and platform access), so packages belong to a task version, not to a pipeline
  node.
  - It is a textarea with one requirement per line, placeholder `requests==2.32.3`.
  - It is shown only for a Python task. Switching a task to SQL with packages set shows the save
    error rather than silently dropping them.
- **Errors:** the API's per-entry errors (`requirements[3]: direct URLs are not allowed`) appear
  under the field, naming the line.
- **Install readiness:** when `GET /api/config` says `pip.available: false`, the field shows "This
  install has no package index configured; tasks with packages will fail to start. Ask your
  operator." The field stays editable.
- **Node and task views:** the read-only node details (`NodeDetails`) and the task detail view list
  the packages, and the task version diff shows changes to them.
- **Run log:** no new component. The install lines are ordinary `system` lines.
- **Types:** `TaskConfig.requirements?: string[]` and `Config.pip`.

**Release:** the builder changes, so this needs **`@projectbooth/pipeline-ui` 1.2.0**: a minor
version, since every change is an addition and an older backend simply omits the fields. booth-design
then needs its pin moved from `1.1.0` to `1.2.0` (by booth-design's owner or the coordinator, not
here). The backend ships as a new booth-pipeline patch tag; chart, app and tag stay equal.

## 9. Where ADR 0115 can't hold as written

**Decision 4** asks for four things:

1. the installer reaches the index only;
2. the default index is PyPI;
3. `allowInternet=false` keeps the task pod closed;
4. the private-range exclusions still apply to the index rule.

Together they can't be met:

- **NetworkPolicy matches IP addresses and selectors, not hostnames.** PyPI is `pypi.org` plus
  `files.pythonhosted.org`, behind a CDN whose addresses are shared and change. A rule narrow enough
  to mean "PyPI only" can't be written. One wide enough to reach PyPI reliably is close to
  `allowInternet`.
- **The policy is per pod.** The init container and the task code share one network identity, so
  whatever the installer can reach, the task's code can reach too. Splitting them would need a
  separate install pod and a volume shared between pods (a PVC), which brings back the shared,
  writable volume that section 6 rules out.
- **The private-range exclusions and an in-cluster mirror are exclusive.** In-cluster pods have
  private addresses, so an exclusion list applied to an in-cluster mirror would block it.

**Recommended ruling:**

- An in-cluster index is the narrow path. It is reached by selector, works with
  `allowInternet=false`, and needs no IP exclusions because it isn't an IP rule.
- Public PyPI needs `allowInternet=true`, which keeps its private-range exclusions. In that mode the
  installer reaches what the task code already reaches; its extra protection is having no
  credentials (Decisions 3 and 6), not a narrower network.

The motivating case, the NFL scrapers, needs `allowInternet=true` anyway to fetch the pages it
scrapes, so this costs that migration nothing.

**Considered and not proposed for v0: a chart-shipped forward proxy.** A small Deployment that
only allows CONNECT to the index's hostnames, with internet egress of its own, and task pods
reaching it by selector. That would make "PyPI only" true with `allowInternet=false`. Against it:

- it is a new long-running component with its own image, patching and NetworkPolicy;
- a hostname allowlist on CONNECT trusts the client's SNI and Host;
- task code could use the proxy too, though only to reach PyPI.

It can be added later beside this design without changing the task model.

Everything else in the ADR holds as written:

- Decisions 1–3, 5 and 6;
- the open questions are answered as follows:
  - per task;
  - no cache in v0;
  - no URLs;
  - an exactly pinned pre-release is allowed, `--pre` is not;
  - the editor lives in the task settings.

## 10. Build order (after agreement)

One PR, merged on green CI and a green Integration run on the branch, then a patch tag:

1. Model, validation, export test.
2. The installer entrypoint and its unit tests.
3. Job composition, readiness wait and failure messages, `/api/config` `pip`.
4. Chart: `runner.pip` values, the egress rule, and the template contract tests.
5. Integration: the index fixture, `pip.sh` with proofs 1–4 and the success case.
6. Builder UI and tests; `web/package.json` to 1.2.0, published by a `pipeline-ui-v1.2.0` tag on
   the same merge commit (publish.yml), beside the booth-pipeline patch tag.

## 11. As built

Where the build differs from the proposal above, and details it didn't spell out:

- **`runner.pip.timeoutSeconds`** (default 300), not `timeout: 5m`, to match the chart's other durations
  (`startTimeoutSeconds`, `credentialSidecar.waitSeconds`).
- **The task's process finds its packages through the harness, not only `PYTHONPATH`.** The harness runs under
  `python -I`, which ignores `PYTHONPATH`, so it adds the site directory itself with `site.addsitedir` (so `.pth` files
  work) ahead of the image's own packages. `PYTHONPATH` is also set, for any Python process the task itself starts.
- **The chart always renders the `pip` container, the `site` volume and the task's `BOOTH_RUNNER_SITE_DIR`;**
  `KubernetesJobRunner` removes all three from a task without requirements, so its Job is identical to before
  (checked against the previous chart's render for every sidecar combination). The runner also refuses, at startup, a
  template whose `pip` container isn't first, runs as a sidecar, mounts anything but `site`, or takes environment
  from a Secret or ConfigMap.
- **The route is worked out by the chart** and given to the API pod as `BOOTH_PIPELINE_PIP_ROUTE`: `index`
  (`runner.pip.egress` set), `internet` (`allowInternet`), `extra` (`egress.extra` non-empty; the chart can't tell
  whether it covers the index, so it doesn't fail fast), `unrestricted` (`networkPolicy.enabled=false`), or empty.
  `GET /api/config` reports `pip: {available, reason | route}`; local development (no task Jobs) reports unavailable.
  Because the route is in the API pod's environment, changing `allowInternet`, `runner.pip.egress` or `egress.extra`
  in a way that changes the route now restarts the API pod (a running task's Job is unaffected).
- **Install output** appears in the task's log as `system` lines prefixed `pip: `, after `installing N Python
  requirement(s) before the task starts: …` and followed by `packages installed in Xs (N MiB)`.
- **An installer that can't run at all** (an unwritable volume, a bad setting) fails with `pip install could not run:
  …`, never a bare traceback.
- **A task with requirements run by the in-process runner** (local development) fails with a message saying packages
  are only installed for task Jobs; it never runs without them.
- **Builder:** there is no task-version diff view to extend; the draft-differs check compares whole configs, so it
  covers requirements. The field is shown for Python tasks (and for any task that already has requirements, so they
  can be removed).
- **Integration** is `tests/integration/pip.sh`, with the index in `tests/integration/fixtures/pypi/`. The probe
  sdist uses an in-tree PEP 517 backend (standard library only), so building it needs nothing from the index but
  itself. It also proves the fast failure with no route (step 0), which section 7 didn't list.

