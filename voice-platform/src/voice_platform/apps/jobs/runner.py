"""Background job runner: knowledge ingestion, evaluation runs, optimization runs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ...config.schema import AgentConfig, PipelineMode, ProviderRef
from ...config.service import AgentService, parse_config
from ...core import jobs
from ...core.db import DbScope
from ...core.models import AgentVersion, EvalResult, EvalRun, EvalScenario, OptimizationRun
from ...core.retention import RETENTION_JOB, sweep
from ...evaluation.evaluators import LLMJudge
from ...evaluation.runner import BenchmarkRunner
from ...evaluation.scenario import Scenario, load_scenarios
from ...knowledge.service import INGEST_JOB
from ...observability.metrics import JOB_DURATION, JOBS
from ...optimization.engine import GateThresholds, Optimizer
from ...optimization.patches import PatchPolicy
from ...optimization.proposers import HeuristicProposer, LLMProposer, Proposer
from ...providers.types import BuildContext, ProviderKind
from ...shared.errors import ConfigurationError, NotFoundError, ValidationFailedError
from ...shared.logging import get_logger, log_context
from ..services import Services

logger = get_logger(__name__)

EVAL_JOB = "evaluation.run"
OPTIMIZE_JOB = "optimization.run"

LLMResolver = Callable[[str, dict[str, Any]], Any]
"""("provider:model" or ProviderRef json, tenant_secrets) → llm.LLM"""


def parse_llm_spec(spec: str) -> ProviderRef:
    provider, _, model = spec.partition(":")
    return ProviderRef(provider=provider, model=model or None)


class JobRunner:
    def __init__(
        self,
        services: Services,
        *,
        llm_resolver: LLMResolver | None = None,
        worker_id: str | None = None,
        poll_s: float = 1.0,
        lease_s: int = 900,
    ) -> None:
        self.s = services
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_s = poll_s
        self.lease_s = lease_s
        self._llm_resolver = llm_resolver
        self.handlers: dict[str, Callable[[jobs.ClaimedJob], Awaitable[dict[str, Any]]]] = {
            INGEST_JOB: self._ingest,
            RETENTION_JOB: self._retention,
            EVAL_JOB: self._evaluate,
            OPTIMIZE_JOB: self._optimize,
        }

    # --- loop --------------------------------------------------------------------------------
    async def schedule_periodic(self) -> None:
        """Enqueue the retention sweep at most once per hour across all runners."""
        async with self.s.db.session(DbScope.system()) as sess:
            await sess.execute(
                sa.text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": RETENTION_JOB},
            )
            recent = await sess.scalar(
                sa.text(
                    "select 1 from jobs where kind = :k and created_at > now() - interval '1 hour'"
                    " limit 1"
                ),
                {"k": RETENTION_JOB},
            )
            if recent is None:
                await jobs.enqueue(
                    sess,
                    kind=RETENTION_JOB,
                    tenant_id=None,
                    payload={},
                    priority=500,
                    max_attempts=2,
                )

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("job runner started", extra={"worker": self.worker_id})
        last_schedule = 0.0
        while not stop.is_set():
            if time.monotonic() - last_schedule > 300:
                last_schedule = time.monotonic()
                try:
                    await self.schedule_periodic()
                except Exception:
                    logger.warning("periodic scheduling failed", exc_info=True)
            try:
                worked = await self.run_once()
            except Exception:
                logger.exception("job loop error")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)

    async def run_once(self) -> bool:
        async with self.s.db.session(DbScope.system()) as sess:
            job = await jobs.claim(
                sess, worker_id=self.worker_id, kinds=list(self.handlers), lease_s=self.lease_s
            )
        if job is None:
            return False
        started = time.perf_counter()
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        with log_context(
            job_id=str(job.id),
            job_kind=job.kind,
            tenant_id=str(job.tenant_id) if job.tenant_id else None,
        ):
            try:
                result = await self.handlers[job.kind](job)
                async with self.s.db.session(DbScope.system()) as sess:
                    await jobs.complete(sess, job.id, result)
                JOBS.labels(job.kind, "succeeded").inc()
            except Exception as e:
                retryable = not isinstance(
                    e, ValidationFailedError | ConfigurationError | NotFoundError
                )
                logger.exception("job failed", extra={"retryable": retryable})
                async with self.s.db.session(DbScope.system()) as sess:
                    status = await jobs.fail(
                        sess, job, f"{type(e).__name__}: {e}", retryable=retryable
                    )
                JOBS.labels(job.kind, status).inc()
            finally:
                heartbeat.cancel()
                JOB_DURATION.labels(job.kind).observe(time.perf_counter() - started)
        return True

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            try:
                async with self.s.db.session(DbScope.system()) as sess:
                    await jobs.extend_lease(sess, job_id, self.worker_id, self.lease_s)
            except Exception:
                logger.warning("lease extension failed", exc_info=True)

    # --- handlers ----------------------------------------------------------------------------
    async def _retention(self, job: jobs.ClaimedJob) -> dict[str, Any]:
        return await sweep(self.s.db, self.s.blobs)

    async def _ingest(self, job: jobs.ClaimedJob) -> dict[str, Any]:
        assert job.tenant_id is not None
        return await self.s.knowledge.process(job.tenant_id, uuid.UUID(job.payload["document_id"]))

    def _llm(self, ref: ProviderRef, secrets: dict[str, Any]) -> Any:
        if self._llm_resolver is not None:
            return self._llm_resolver(f"{ref.provider}:{ref.model or ''}", secrets)
        return self.s.registry.create(ProviderKind.LLM, ref, BuildContext(tenant_secrets=secrets))

    async def _tenant_secrets(self, tenant_id: uuid.UUID, cfg: AgentConfig) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if cfg.pipeline.llm is None:
            return out
        async with self.s.db.session(DbScope.for_tenant(tenant_id)) as sess:
            for ref in cfg.pipeline.llm.chain():
                if ref.credential and ref.credential not in out:
                    _k, payload = await self.s.vault.resolve_by_name(
                        sess, tenant_id=tenant_id, name=ref.credential
                    )
                    out[ref.credential] = payload
        return out

    async def _scenarios(
        self, tenant_id: uuid.UUID, suite: str, slugs: list[str]
    ) -> list[Scenario]:
        found: dict[str, Scenario] = {}
        lib = Path(self.s.settings.scenario_library_path) / suite
        if lib.exists():
            for sc in load_scenarios(lib):
                found[sc.id] = sc
        async with self.s.db.session(DbScope.for_tenant(tenant_id)) as sess:
            for row in (
                await sess.scalars(
                    sa.select(EvalScenario).where(EvalScenario.tenant_id == tenant_id)
                )
            ).all():
                found[row.slug] = Scenario.model_validate(row.spec)
        chosen = [found[s] for s in slugs if s in found] if slugs else list(found.values())
        if not chosen:
            raise ValidationFailedError("no scenarios selected")
        return chosen

    def _runner(
        self, cfg: AgentConfig, secrets: dict[str, Any], *, use_judge: bool
    ) -> tuple[BenchmarkRunner, dict[str, Any]]:
        if cfg.pipeline.mode is not PipelineMode.CASCADED or cfg.pipeline.llm is None:
            raise ValidationFailedError("text-mode evaluation requires a cascaded pipeline")
        agent_ref = cfg.pipeline.llm.chain()[0]
        notes: dict[str, Any] = {"agent_llm": f"{agent_ref.provider}:{agent_ref.model}"}
        caller_ref = parse_llm_spec(self.s.settings.simulator_llm)
        judge = None
        if use_judge:
            judge_ref = parse_llm_spec(self.s.settings.judge_llm)
            if (judge_ref.provider, judge_ref.model) == (agent_ref.provider, agent_ref.model):
                notes["judge"] = "skipped: judge model equals the agent model (independence)"
            else:
                judge = LLMJudge(self._llm(judge_ref, {}), model_label=self.s.settings.judge_llm)
                notes["judge"] = self.s.settings.judge_llm
        runner = BenchmarkRunner(
            agent_llm_factory=lambda c: self._llm(c.pipeline.llm.chain()[0], secrets),  # type: ignore[union-attr]
            caller_llm=self._llm(caller_ref, {}),
            judge=judge,
            concurrency=self.s.settings.eval_max_concurrency,
        )
        return runner, notes

    async def _evaluate(self, job: jobs.ClaimedJob) -> dict[str, Any]:
        assert job.tenant_id is not None
        tid = job.tenant_id
        run_id = uuid.UUID(job.payload["run_id"])
        async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
            run = await sess.get(EvalRun, run_id)
            if run is None:
                raise NotFoundError("evaluation run not found")
            version = await AgentService(sess, tid, None).get_version(run.agent_version_id)
            cfg = parse_config(version.config).materialize("staging")
            run.status, run.started_at = "running", datetime.now(UTC)
            suite, seed, reps = run.suite, run.seed, run.repetitions
            slugs = list(run.settings.get("scenario_slugs") or [])
            use_judge = bool(run.settings.get("use_judge", True))
        try:
            scenarios = await self._scenarios(tid, suite, slugs)
            secrets = await self._tenant_secrets(tid, cfg)
            runner, notes = self._runner(cfg, secrets, use_judge=use_judge)
            result = await runner.run(cfg, scenarios, repetitions=reps, seed=seed)
        except Exception as e:
            async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
                await sess.execute(
                    sa.update(EvalRun)
                    .where(EvalRun.id == run_id)
                    .values(
                        status="failed",
                        error=f"{type(e).__name__}: {e}"[:2000],
                        finished_at=datetime.now(UTC),
                    )
                )
            raise
        summary = {**result.summary(), "notes": notes}
        async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
            sess.add_all(
                [EvalResult(tenant_id=tid, run_id=run_id, **rec.as_row()) for rec in result.records]
            )
            await sess.execute(
                sa.update(EvalRun)
                .where(EvalRun.id == run_id)
                .values(status="succeeded", summary=summary, finished_at=datetime.now(UTC))
            )
        return {"pass_rate": summary["pass_rate"], "runs": summary["runs"]}

    async def _optimize(self, job: jobs.ClaimedJob) -> dict[str, Any]:
        assert job.tenant_id is not None
        tid = job.tenant_id
        opt_id = uuid.UUID(job.payload["optimization_run_id"])
        async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
            opt = await sess.get(OptimizationRun, opt_id)
            if opt is None:
                raise NotFoundError("optimization run not found")
            baseline = await AgentService(sess, tid, None).get_version(opt.baseline_version_id)
            base_cfg = parse_config(baseline.config)
            settings = dict(opt.settings)
            opt.status = "running"
            agent_id, baseline_id = opt.agent_id, baseline.id
        try:
            eval_cfg = base_cfg.materialize("staging")
            scenarios = await self._scenarios(
                tid, "receptionist", list(settings.get("scenario_slugs") or [])
            )
            secrets = await self._tenant_secrets(tid, eval_cfg)
            runner, notes = self._runner(eval_cfg, secrets, use_judge=False)
            allowed = {k: tuple(v) for k, v in (settings.get("allowed_models") or {}).items()}
            proposers: list[Proposer] = [HeuristicProposer()]
            if self.s.settings.optimizer_llm:
                proposers.append(
                    LLMProposer(
                        self._llm(parse_llm_spec(self.s.settings.optimizer_llm), {}),
                        allowed_models=allowed,
                    )
                )
            optimizer = Optimizer(
                runner,
                proposers,
                policy=PatchPolicy(allowed_models=allowed),
                thresholds=GateThresholds(min_pass_rate=float(settings.get("min_pass_rate", 0.8))),
                repetitions=int(settings.get("repetitions", 1)),
                seed=int(settings.get("seed", 0)),
                max_iterations=int(settings.get("max_iterations", 3)),
            )
            outcome = await optimizer.run(eval_cfg, scenarios)
        except Exception as e:
            async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
                await sess.execute(
                    sa.update(OptimizationRun)
                    .where(OptimizationRun.id == opt_id)
                    .values(
                        status="failed",
                        error=f"{type(e).__name__}: {e}"[:2000],
                        finished_at=datetime.now(UTC),
                    )
                )
            raise
        candidate_id = None
        async with self.s.db.session(DbScope.for_tenant(tid)) as sess:
            if outcome.best_config is not None:
                # Re-apply only the accepted patches on the *original* (non-materialized)
                # config so environment overrides are preserved.
                from ...optimization.patches import apply_patches

                accepted = [p for it in outcome.iterations if it.kept for p in it.patches_applied]
                cand_cfg, _applied, _rej = apply_patches(
                    base_cfg, accepted, PatchPolicy(allowed_models=allowed)
                )
                v = await AgentService(sess, tid, None).create_draft(
                    agent_id,
                    cand_cfg.model_dump(mode="json"),
                    parent_version_id=baseline_id,
                    source="optimizer",
                    status="candidate",
                    notes=f"optimizer run {opt_id}: decision {outcome.decision}",
                )
                candidate_id = v.id
            await sess.execute(
                sa.update(OptimizationRun)
                .where(OptimizationRun.id == opt_id)
                .values(
                    status="succeeded",
                    decision=outcome.decision,
                    report={**outcome.report(), "notes": notes},
                    report_markdown=outcome.markdown(),
                    iterations=[i.as_dict() for i in outcome.iterations],
                    best_candidate_version_id=candidate_id,
                    finished_at=datetime.now(UTC),
                )
            )
        return {
            "decision": outcome.decision,
            "candidate_version_id": str(candidate_id) if candidate_id else None,
        }


async def main() -> None:
    import signal

    from ...shared.logging import configure_logging
    from ...shared.settings import get_settings

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json, service="vp-jobs")
    from ...observability.tracing_setup import configure_tracing

    configure_tracing(
        service="vp-jobs",
        endpoint=settings.otel_exporter_otlp_endpoint,
        environment=settings.environment.value,
    )
    services = Services.from_settings(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)  # graceful: finish the current job, then exit
    try:
        await JobRunner(services).run_forever(stop)
    finally:
        await services.aclose()


__all__ = ["AgentVersion", "JobRunner", "main"]
