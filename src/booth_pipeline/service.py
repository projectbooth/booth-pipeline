"""The application service: every use case, independent of HTTP.

``api.py`` translates HTTP to these calls and their exceptions to status codes; the scheduler
calls ``start_run`` directly. Keeping the rules here — not in route handlers — is what lets the
scheduler and the API share exactly one implementation of "start a run".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .auth import Identity
from .catalog_client import CatalogClient, CatalogDenied, CatalogNotFound, CatalogUnavailable
from .engine import compile_job
from .export import export_pipeline_version
from .model import (
    BASE_RUNNER,
    CatalogCode,
    EventTrigger,
    InlineCode,
    ModelError,
    PipelineSpec,
    StorageCode,
    TaskConfig,
    Trigger,
    code_language,
    sha256_text,
    validate_structure,
    validate_task_config,
)
from .owners import is_service_account, parse_service_account
from .records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    TRIGGER_EVENT,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    LogLine,
    Page,
    Pipeline,
    PipelineDraft,
    PipelineVersion,
    Run,
    TaskDraft,
    TaskEntity,
    TaskRun,
    TaskVersionRecord,
)
from .resolve import resolve_task_configs, resolve_task_ref
from .runners import languages
from .runners.base import Cancellation
from .runners.registry import RunnerRegistry
from .runners.spark import SPARK_STREAM
from .runs import RunManager
from .schedule import next_fire_trigger
from .service_accounts import ServiceAccountsClient, ServiceAccountsUnavailable
from .storage_client import StorageClient, StorageDenied, StorageNotFound, StorageUnavailable
from .store.base import Busy, Store

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class Forbidden(Exception):
    """The caller's role doesn't allow this (403), e.g. an editor choosing a service account owner."""


class Unavailable(Exception):
    """A dependency the request needs (the catalog) is unreachable — retryable, not the caller's fault."""


class PipelineBusy(Exception):
    """The pipeline already has an active run and does not allow concurrent runs."""


class _NullRecorder:
    """Compiling at save time needs a recorder only to satisfy the signature; nothing runs."""

    def task_started(self, key, attempt): ...
    def task_attempt_finished(self, key, attempt, status, error): ...
    def task_output(self, key, attempt, output): ...
    def task_log(self, key, attempt): ...
    def system(self, message): ...


@dataclass
class PipelineService:
    store: Store
    registry: RunnerRegistry
    catalog: CatalogClient
    manager: RunManager
    storage: StorageClient
    # ADR 0120: core's service accounts, read as the caller (checked when one is named as an owner).
    accounts: ServiceAccountsClient | None = None
    # ADR 0123: whether this module holds an event-bus credential, without which no event trigger fires. A
    # callable (the app's EventBus.configured, read each time: the Secret may arrive later) or a fixed bool.
    event_bus: Any = False

    # ---- pipelines ----
    def create_pipeline(self, ident: Identity, name: str, description: str, spec: PipelineSpec | None, notes: str) -> tuple[Pipeline, PipelineVersion | None]:
        if spec is not None:
            # Validate BEFORE creating anything, so a bad first version does not leave an empty pipeline behind.
            spec = self._prepare(ident, spec)
        p = self.store.create_pipeline(ident.workspace, name.strip(), description.strip(), ident.display_name)
        v = self.store.add_version(ident.workspace, p.id, spec, notes, ident.display_name) if spec is not None else None
        if spec is not None:
            self.store.save_pipeline_draft(ident.workspace, p.id, spec, ident.display_name)
        return (self.store.get_pipeline(ident.workspace, p.id) or p), v

    def get_pipeline(self, ident: Identity, pipeline_id: str) -> Pipeline:
        return self._need(self.store.get_pipeline(ident.workspace, pipeline_id), "pipeline")

    def list_pipelines(self, ident: Identity, q: str, limit: int, offset: int) -> Page:
        return self.store.list_pipelines(ident.workspace, q, limit, offset)

    def update_pipeline(self, ident: Identity, pipeline_id: str, name: str, description: str) -> Pipeline:
        return self._need(self.store.update_pipeline(ident.workspace, pipeline_id, name.strip(), description.strip()), "pipeline")

    def delete_pipeline(self, ident: Identity, pipeline_id: str) -> None:
        self.get_pipeline(ident, pipeline_id)
        # Stop any run this process is still executing first: deletion cascades the run's own rows
        # (ADR 0071 — there is no more separate Job to have done this instead), so an orphaned
        # worker would keep going with nothing left to record its result against.
        self._cancel_active_runs(ident, pipeline_id)
        if not self.store.delete_pipeline(ident.workspace, pipeline_id):
            raise NotFound("pipeline not found")

    def _cancel_active_runs(self, ident: Identity, pipeline_id: str) -> None:
        for r in self.store.list_runs(ident.workspace, pipeline_id, None, 1000, 0).items:
            if r.status in RUN_ACTIVE:
                self.store.request_cancel(ident.workspace, r.id)
                self.manager.cancel(r.id)

    def save_version(self, ident: Identity, pipeline_id: str, spec: PipelineSpec, notes: str) -> PipelineVersion:
        self.get_pipeline(ident, pipeline_id)
        prepared = self._prepare(ident, spec)
        v = self._need(self.store.add_version(ident.workspace, pipeline_id, prepared, notes, ident.display_name), "pipeline")
        # ADR 0073: a version-save also refreshes the draft, so it is never behind the latest
        # immutable version — a plain Save right after "Save as new version" starts from it, not
        # from something older.
        self.store.save_pipeline_draft(ident.workspace, pipeline_id, prepared, ident.display_name)
        return v

    def save_pipeline_draft(self, ident: Identity, pipeline_id: str, spec: PipelineSpec) -> PipelineDraft:
        """Plain "Save" (ADR 0073): persists the draft in place, no version created. Goes through
        the same validate -> check-refs -> compile gate as "Save as new version" — a broken draft
        would also break anything resolving an "Always follow latest" reference to it live."""
        self.get_pipeline(ident, pipeline_id)
        prepared = self._prepare(ident, spec)
        return self._need(self.store.save_pipeline_draft(ident.workspace, pipeline_id, prepared, ident.display_name), "pipeline")

    def get_pipeline_draft(self, ident: Identity, pipeline_id: str) -> PipelineDraft | None:
        self.get_pipeline(ident, pipeline_id)
        return self.store.get_pipeline_draft(ident.workspace, pipeline_id)

    def get_version(self, ident: Identity, pipeline_id: str, version: int | None) -> PipelineVersion:
        return self._need(self.store.get_version(ident.workspace, pipeline_id, version), "pipeline version")

    def list_versions(self, ident: Identity, pipeline_id: str, limit: int, offset: int) -> Page:
        self.get_pipeline(ident, pipeline_id)
        return self.store.list_versions(ident.workspace, pipeline_id, limit, offset)

    def export_version(self, ident: Identity, pipeline_id: str, version: int | None) -> str:
        """A pipeline version as YAML text (ADR 0071 phase 4) — export-only, but a faithful,
        complete structure (every referenced task resolved to its own name/description/version/
        config), shaped as the intended future import format too."""
        pipeline = self.get_pipeline(ident, pipeline_id)
        v = self.get_version(ident, pipeline_id, version)
        return export_pipeline_version(self.store, ident.workspace, pipeline, v)

    def validate_draft(self, ident: Identity, spec: PipelineSpec) -> None:
        """The canvas's live check: DAG structure, that every referenced task version exists, and
        that the whole thing compiles. No side effects, and no catalog/storage call — a task
        version's code is already snapshotted at its own save time (ADR 0071), so resolving a
        reference to it is a pure store read. Raises ``ModelError``."""
        validate_structure(spec)
        self._check_task_refs(ident, spec)
        configs = resolve_task_configs(self.store, ident.workspace, spec)
        compile_job(spec, configs, run_id="validate", registry=self.registry, recorder=_NullRecorder(), cancel=Cancellation())

    # ---- the save pipeline: validate structure -> check task refs resolve -> compile ----
    def _prepare(self, ident: Identity, spec: PipelineSpec) -> PipelineSpec:
        validate_structure(spec)
        self._check_task_refs(ident, spec)
        configs = resolve_task_configs(self.store, ident.workspace, spec)
        # Compile now, not at run time: a structural problem must surface when the user saves,
        # not at 3am when the scheduler fires. (Nothing executes.)
        compile_job(spec, configs, run_id="validate", registry=self.registry, recorder=_NullRecorder(), cancel=Cancellation())
        return spec

    def _check_task_refs(self, ident: Identity, spec: PipelineSpec) -> None:
        """Confirm every reference (pinned or "latest") actually resolves to something right now —
        a real store lookup ``validate_structure`` cannot do on its own (ADR 0071). Unlike before
        ADR 0073, this never rewrites ``"latest"`` into a concrete version number: a saved spec —
        draft or immutable version alike — keeps a literal ``"latest"`` forever, so it keeps
        picking up whatever the referenced task's draft or latest version is at USE time (validate,
        save-time compile-check here, and run start in ``runs.py``), not frozen at save time. A
        version pin is completely unaffected either way."""
        for i, ref in enumerate(spec.tasks):
            resolve_task_ref(self.store, ident.workspace, ref, f"tasks[{i}]")

    # ---- pipeline scheduling (folded from the retired Job entity, ADR 0071) ----
    def update_schedule(
        self,
        ident: Identity,
        pipeline_id: str,
        schedule: Trigger | None,
        allow_concurrent_runs: bool,
        role_ceiling: str,
        pinned_version: int | None,
        event_trigger: EventTrigger | None = None,
        keep_event_trigger: bool = False,
    ) -> Pipeline:
        p = self.get_pipeline(ident, pipeline_id)
        if keep_event_trigger:
            event_trigger = p.event_trigger
        if schedule is not None and schedule.enabled and p.latest_version == 0:
            raise ModelError("the pipeline has no saved version to run — save it in the builder first", "schedule")
        if event_trigger is not None and event_trigger.enabled:
            if p.latest_version == 0:
                raise ModelError("the pipeline has no saved version to run — save it in the builder first", "eventTrigger")
            if not (self.event_bus() if callable(self.event_bus) else self.event_bus):
                raise ModelError(
                    "event triggers need the platform's event bus, and this module has no bus credential (booth-core grants one from the module's manifest)",
                    "eventTrigger",
                )
            self._check_event_backends(ident, p.event_trigger, event_trigger)
        if pinned_version is not None and self.store.get_version(ident.workspace, pipeline_id, pinned_version) is None:
            raise ModelError(f"version {pinned_version} does not exist", "pinnedVersion")
        p.schedule = schedule
        p.allow_concurrent_runs = allow_concurrent_runs
        p.role_ceiling = role_ceiling
        p.pinned_version = pinned_version
        p.next_run_at = _next(schedule, datetime.now(UTC))
        # Events published before the trigger was turned on, or before its match last changed, are
        # not its business (a turned-on trigger must not fire for a burst it was never watching).
        if event_trigger is not None and event_trigger.enabled and (
            p.event_trigger is None or not p.event_trigger.enabled or p.event_trigger.match != event_trigger.match
        ):
            p.event_trigger_since = datetime.now(UTC)
        p.event_trigger = event_trigger
        # The owner stays whoever first gave the pipeline a schedule or an event trigger; after that
        # it changes only through set_owner (Take over, or a workspace owner choosing a service account).
        if not p.owner_sub:
            p.owner_sub, p.owner_name = ident.subject, ident.display_name
        return self._need(self.store.update_schedule(p), "pipeline")

    def _check_event_backends(self, ident: Identity, before: EventTrigger | None, after: EventTrigger) -> None:
        """Every Storage backend the trigger watches must exist, asked as the caller. Only backends new
        to this save are asked about, so an unrelated edit still saves while booth-storage is down.
        Reading needs no more than workspace membership today (ADR 0123 revision), which every
        event-fired run's start-time authorization already checks for the owner."""
        known = {m.storage.backend_id for m in (before.match if before else []) if m.storage}
        for i, m in enumerate(after.match):
            if m.storage is None or m.storage.backend_id in known:
                continue
            try:
                exists = self.storage.backend_exists(ident.token, ident.workspace, m.storage.backend_id)
            except StorageDenied as e:
                raise Forbidden(f"booth-storage refused to say whether backend {m.storage.backend_id!r} exists: {e}") from None
            except StorageUnavailable as e:
                raise Unavailable(f"could not check backend {m.storage.backend_id!r} with booth-storage: {e}") from None
            if not exists:
                raise ModelError(f"this workspace has no Storage backend {m.storage.backend_id!r}", f"eventTrigger.match[{i}].storage.backendId")
            known.add(m.storage.backend_id)

    def set_owner(self, ident: Identity, pipeline_id: str, owner: str) -> Pipeline:
        """Take over (``owner == "me"``) or give the pipeline to a service account (owners.py's rules)."""
        p = self.get_pipeline(ident, pipeline_id)
        is_ws_owner = ident.role == "owner"
        if owner == "me":
            if is_service_account(p.owner_sub) and not is_ws_owner:
                raise Forbidden("only a workspace owner can take over a pipeline that runs as a service account")
            p.owner_sub, p.owner_name = ident.subject, ident.display_name
        elif is_service_account(owner):
            if not is_ws_owner:
                raise Forbidden("only a workspace owner can make a service account a pipeline's owner")
            ref = parse_service_account(owner)
            if ref is None:
                raise ModelError(f"{owner!r} is not a service account id (svc:<workspace>:<name>)", "owner")
            if ref.workspace != ident.workspace:
                raise ModelError(f"{owner!r} belongs to another workspace; choose one of this workspace's service accounts", "owner")
            self._check_service_account(ident, ref.name)
            p.owner_sub, p.owner_name = ref.id, ref.name
        else:
            raise ModelError('a pipeline can be taken over ("me") or given to a service account (svc:<workspace>:<name>), never handed to another person', "owner")
        return self._need(self.store.update_schedule(p), "pipeline")

    def _check_service_account(self, ident: Identity, name: str) -> None:
        """The account must exist in core and be enabled before a pipeline is given to it."""
        if self.accounts is None:
            raise Unavailable("service accounts are booth-core's, and this module can't ask it (no core.url)")
        try:
            account = self.accounts.get(ident.token, ident.workspace, name)
        except ServiceAccountsUnavailable as e:
            raise Unavailable(f"could not check service account {name!r} with booth-core: {e}") from None
        if account is None:
            raise ModelError(f"this workspace has no service account named {name!r}", "owner")
        if account.disabled:
            raise ModelError(f"service account {name!r} is disabled; enable it in the workspace settings before giving it a pipeline", "owner")

    def service_account_uses(self, ident: Identity) -> list[tuple[str, list[Pipeline]]]:
        """Which of this workspace's pipelines run as each service account (ADR 0120 addendum 2:
        core keeps no usage record, so this module shows it). Sorted by account id."""
        uses: dict[str, list[Pipeline]] = {}
        offset = 0
        while True:
            page = self.store.list_pipelines(ident.workspace, "", 500, offset)
            for p in page.items:
                if is_service_account(p.owner_sub):
                    uses.setdefault(p.owner_sub, []).append(p)
            offset += len(page.items)
            if not page.items or offset >= page.total:
                break
        return sorted(uses.items())

    # ---- tasks (ADR 0071: standalone, versioned, reusable — the direct counterpart of pipelines) ----
    def create_task(self, ident: Identity, name: str, description: str, config: TaskConfig | None, notes: str) -> tuple[TaskEntity, TaskVersionRecord | None]:
        if config is not None:
            config = self._prepare_task_config(ident, config, previous=None)
        t = self.store.create_task(ident.workspace, name.strip(), description.strip(), ident.display_name)
        v = self.store.add_task_version(ident.workspace, t.id, config, notes, ident.display_name) if config is not None else None
        if config is not None:
            self.store.save_task_draft(ident.workspace, t.id, config, ident.display_name)
        return (self.store.get_task(ident.workspace, t.id) or t), v

    def get_task(self, ident: Identity, task_id: str) -> TaskEntity:
        return self._need(self.store.get_task(ident.workspace, task_id), "task")

    def list_tasks(self, ident: Identity, q: str, limit: int, offset: int) -> Page:
        return self.store.list_tasks(ident.workspace, q, limit, offset)

    def update_task(self, ident: Identity, task_id: str, name: str, description: str) -> TaskEntity:
        return self._need(self.store.update_task(ident.workspace, task_id, name.strip(), description.strip()), "task")

    def delete_task(self, ident: Identity, task_id: str) -> None:
        if not self.store.delete_task(ident.workspace, task_id):
            raise NotFound("task not found")

    def save_task_version(self, ident: Identity, task_id: str, config: TaskConfig, notes: str) -> TaskVersionRecord:
        self.get_task(ident, task_id)
        previous = self._latest_task_config(ident, task_id)
        prepared = self._prepare_task_config(ident, config, previous)
        v = self._need(self.store.add_task_version(ident.workspace, task_id, prepared, notes, ident.display_name), "task")
        # ADR 0073: also refreshes the draft, so it is never behind the latest immutable version.
        self.store.save_task_draft(ident.workspace, task_id, prepared, ident.display_name)
        return v

    def save_task_draft(self, ident: Identity, task_id: str, config: TaskConfig) -> TaskDraft:
        """Plain "Save" (ADR 0073): persists the draft in place, no version created."""
        self.get_task(ident, task_id)
        previous = self._latest_task_config(ident, task_id)
        prepared = self._prepare_task_config(ident, config, previous)
        return self._need(self.store.save_task_draft(ident.workspace, task_id, prepared, ident.display_name), "task")

    def get_task_draft(self, ident: Identity, task_id: str) -> TaskDraft | None:
        self.get_task(ident, task_id)
        return self.store.get_task_draft(ident.workspace, task_id)

    def _latest_task_config(self, ident: Identity, task_id: str) -> TaskConfig | None:
        """The baseline a code reference is reused against without re-fetching (ADR 0063's "reuse
        rule" — see ``_resolve_task_code``): the current draft if there is one (ADR 0073 — it is
        always at least as fresh as the latest version), else the latest saved version, else None
        for a task that has never been configured at all."""
        draft = self.store.get_task_draft(ident.workspace, task_id)
        if draft is not None:
            return draft.config
        v = self.store.get_task_version(ident.workspace, task_id, None)
        return v.config if v else None

    def get_task_version(self, ident: Identity, task_id: str, version: int | None) -> TaskVersionRecord:
        return self._need(self.store.get_task_version(ident.workspace, task_id, version), "task version")

    def list_task_versions(self, ident: Identity, task_id: str, limit: int, offset: int) -> Page:
        self.get_task(ident, task_id)
        return self.store.list_task_versions(ident.workspace, task_id, limit, offset)

    # ---- the save task version: validate -> resolve code -> validate again ----
    def _prepare_task_config(self, ident: Identity, config: TaskConfig, previous: TaskConfig | None) -> TaskConfig:
        validate_task_config(config, self.registry.available_ids())
        if config.runner != BASE_RUNNER:
            reason = self.registry.availability(config.runner, ident.token, ident.workspace)
            if reason:
                raise ModelError(f"{config.runner} is not available in this workspace: {reason}", "runner")
        resolved = self._resolve_task_code(ident, config, previous)
        validate_task_config(resolved, self.registry.available_ids())  # re-check: the byte budget now includes catalog/storage source
        return resolved

    def _resolve_task_code(self, ident: Identity, config: TaskConfig, previous: TaskConfig | None) -> TaskConfig:
        # A snapshot we already stored ourselves is trusted; anything a client *sends* claiming to
        # be a resolved snapshot is not (it would let a caller label arbitrary source as "catalog
        # entry X @ v1", or an arbitrary storage object as already fetched). So a reference is
        # re-fetched unless it matches this task's own previous version — which also lets an
        # unrelated edit be saved while the catalog or storage is down (see StorageCode's own
        # docstring in model.py for why that reuse rule is correct for storage too, despite a
        # storage object having no immutable version the way a catalog entry does).
        field = "code"
        code = config.code
        if isinstance(code, InlineCode):
            code = InlineCode(source=code.source, sha256=sha256_text(code.source))
        else:
            known = previous.code if previous and isinstance(previous.code, CatalogCode | StorageCode) and previous.code.resolved else None
            snap = known if known is not None and _ref_key(known) == _ref_key(code) else None
            if snap is None:
                snap = self._fetch_catalog(ident, code, field) if isinstance(code, CatalogCode) else self._fetch_storage(ident, code, field)
            code = snap
            if config.runner == BASE_RUNNER and code_language(code) not in languages.available():
                raise ModelError(
                    f"the code at {field} is {code_language(code)!r}; the base runner supports: {', '.join(languages.available())}",
                    field,
                )
        return config.model_copy(update={"code": code})

    def _fetch_catalog(self, ident: Identity, ref: CatalogCode, field: str) -> CatalogCode:
        try:
            got = self.catalog.fetch(ident.token, ident.workspace, ref.entry_id, ref.version)
        except CatalogNotFound:
            raise ModelError(f"catalog code entry {ref.entry_id!r} version {ref.version!r} was not found", field) from None
        except CatalogDenied:
            raise ModelError("you are not permitted to read that catalog code entry", field) from None
        except CatalogUnavailable as e:
            raise Unavailable(f"cannot resolve catalog code at {field}: {e}. Inline code needs no catalog; otherwise retry shortly.") from e
        return CatalogCode(
            entry_id=got.entry_id,
            version=got.version,
            name=got.entry_name,
            language=got.language,
            sha256=sha256_text(got.source),
            source=got.source,
        )

    def _fetch_storage(self, ident: Identity, ref: StorageCode, field: str) -> StorageCode:
        try:
            got = self.storage.fetch(ident.token, ident.workspace, ref.backend_id, ref.path)
        except StorageNotFound:
            raise ModelError(f"no object at {ref.path!r} in storage backend {ref.backend_id!r}", field) from None
        except StorageDenied:
            raise ModelError("you are not permitted to read that storage object", field) from None
        except StorageUnavailable as e:
            raise Unavailable(f"cannot resolve storage code at {field}: {e}. Inline code needs no storage; otherwise retry shortly.") from e
        return StorageCode(
            backend_id=got.backend_id,
            path=got.path,
            name=got.path.rsplit("/", 1)[-1],
            language=got.language or None,
            sha256=sha256_text(got.source),
            source=got.source,
        )

    # ---- runs ----
    def start_run(
        self, workspace: str, pipeline: Pipeline, trigger: str, by: str, owner_sub: str, owner_name: str = "", trigger_events: dict | None = None
    ) -> Run:
        """The single implementation of "start a run": used by Run-now and by the scheduler.

        Runs the pipeline's `pinned_version` if it has one, otherwise the latest saved version —
        mirrors the old Job's `pipeline_version` pin, now living on Pipeline directly (ADR 0071).
        """
        version = self.store.get_version(workspace, pipeline.id, pipeline.pinned_version)
        if version is None:
            raise ModelError("the pipeline has no saved version to run", "pipeline")
        run = Run(
            id=str(uuid4()),
            workspace=workspace,
            pipeline_id=pipeline.id,
            pipeline_version=version.version,  # pinned NOW, once, for this run
            status=RUN_QUEUED,
            trigger=trigger,
            triggered_by=by,
            created_at=datetime.now(UTC),
            owner_sub=owner_sub,
            owner_name=owner_name,
            trigger_events=trigger_events,
        )
        try:
            self.store.create_run(run, [t.key for t in version.spec.tasks], exclusive=not pipeline.allow_concurrent_runs)
        except Busy:
            raise PipelineBusy("this pipeline already has an active run; wait for it to finish, cancel it, or allow concurrent runs") from None
        self.manager.submit(run)
        return run

    def run_now(self, ident: Identity, pipeline_id: str) -> Run:
        # A manual run belongs to the person who pressed the button: they are present and
        # authenticated, so their live role (not a possibly-lapsed owner) is what caps it.
        return self.start_run(ident.workspace, self.get_pipeline(ident, pipeline_id), TRIGGER_MANUAL, ident.display_name, ident.subject, ident.display_name)

    def start_scheduled(self, pipeline: Pipeline) -> Run | None:
        try:
            return self.start_run(pipeline.workspace, pipeline, TRIGGER_SCHEDULE, "schedule", pipeline.owner_sub, pipeline.owner_name)
        except PipelineBusy:
            log.info("scheduled run of pipeline %s skipped: previous run still active", pipeline.id)
        except ModelError as e:
            log.warning("scheduled run of pipeline %s could not start: %s", pipeline.id, e.message)
        return None

    def start_event_fired(self, pipeline: Pipeline, fired: dict) -> Run | None:
        """A claimed event-trigger fire (ADR 0123). Acts as the owner, exactly as a scheduled run does."""
        try:
            return self.start_run(
                pipeline.workspace, pipeline, TRIGGER_EVENT, describe_trigger_events(fired), pipeline.owner_sub, pipeline.owner_name, fired
            )
        except PipelineBusy:
            # A run started between the claim and this: owe a follow-up (queue_one) or drop it (skip).
            log.info("event-fired run of pipeline %s found it busy; %s", pipeline.id, "queued" if pipeline.event_trigger and pipeline.event_trigger.when_running == "queue_one" else "skipped")
            self.store.requeue_event_trigger(pipeline.id, fired, datetime.now(UTC))
        except ModelError as e:
            log.warning("event-fired run of pipeline %s could not start: %s", pipeline.id, e.message)
        return None

    def event_trigger_state(self, ident: Identity, pipeline_id: str):
        p = self.get_pipeline(ident, pipeline_id)
        return p, self.store.get_event_trigger_state(ident.workspace, p.id)

    def get_run(self, ident: Identity, run_id: str) -> tuple[Run, list[TaskRun]]:
        run = self._need(self.store.get_run(ident.workspace, run_id), "run")
        return run, self.store.list_task_runs(run.id)

    def list_runs(self, ident: Identity, pipeline_id: str | None, status: str | None, limit: int, offset: int) -> Page:
        return self.store.list_runs(ident.workspace, pipeline_id, status, limit, offset)

    def cancel_run(self, ident: Identity, run_id: str) -> Run:
        run = self._need(self.store.request_cancel(ident.workspace, run_id), "run")
        self.manager.cancel(run_id)
        return run

    def logs(self, ident: Identity, run_id: str, task_key: str | None, after_seq: int, limit: int) -> list[LogLine]:
        self._need(self.store.get_run(ident.workspace, run_id), "run")
        # ADR 0119 amendment 3 point 4: a Spark driver's log can carry whatever its code prints (its
        # own token, its S3 lease keys), so, as booth-spark itself rules, viewers don't get it.
        hidden = () if ident.can_write else (SPARK_STREAM,)
        return self.store.list_logs(run_id, task_key, after_seq, limit, hidden)

    @staticmethod
    def _need(value, what: str):
        if value is None:
            raise NotFound(f"{what} not found")
        return value


def _next(schedule: Trigger | None, now: datetime) -> datetime | None:
    return next_fire_trigger(schedule, now) if schedule and schedule.enabled else None


def _ref_key(code: CatalogCode | StorageCode) -> tuple[str, str, str]:
    """What makes two code references "the same" for reuse-without-refetching (ADR 0063): a
    catalog reference by ``(entryId, version)``, a storage reference by ``(backendId, path)`` —
    each tagged with its own type so the two families can never collide."""
    if isinstance(code, CatalogCode):
        return ("catalog", code.entry_id, code.version)
    return ("storage", code.backend_id, code.path)


def describe_trigger_events(fired: dict) -> str:
    """An event-fired run's "triggered by": "3 files", "2 files, 1 write". Deliberately without paths or
    API names, which only editors and owners see (ADR 0123 revision); the run's triggerEvents has them."""
    events, count = fired.get("events") or [], int(fired.get("count") or 0)
    if count == 0:
        return "events may have been missed" if fired.get("missedEvents") else "event"
    files = sum(1 for e in events if e.get("type") == "storage.object.created")
    writes = sum(1 for e in events if e.get("type") == "api.write.committed")
    parts = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in ((files, "file"), (writes, "write")) if n]
    more = count - len(events)
    return ", ".join(parts) + (f" and {more} more" if more > 0 else "")
