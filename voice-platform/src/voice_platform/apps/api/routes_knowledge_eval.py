"""Knowledge bases, evaluation scenarios/runs, optimization runs and approvals."""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, ValidationError

from ...config.service import AgentService
from ...core import jobs
from ...core.audit import record_audit
from ...core.models import (
    AgentVersion,
    EvalResult,
    EvalRun,
    EvalScenario,
    KnowledgeBase,
    KnowledgeDocument,
    OptimizationRun,
)
from ...core.rbac import Permission as P
from ...evaluation.scenario import Scenario
from ...shared.errors import ConflictError, NotFoundError, ValidationFailedError
from .deps import TenantCtx, tenant_ctx

router = APIRouter(prefix="/v1/tenants/{tenant_id}")

EVAL_JOB = "evaluation.run"
OPTIMIZE_JOB = "optimization.run"


# --- knowledge --------------------------------------------------------------------------------
class KbIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    embedding_provider: Literal["openai", "hashing"] = "openai"
    embedding_model: str = "text-embedding-3-small"


def _kb(kb: KnowledgeBase) -> dict[str, Any]:
    return {
        "id": str(kb.id),
        "name": kb.name,
        "description": kb.description,
        "embedding_provider": kb.embedding_provider,
        "embedding_model": kb.embedding_model,
    }


def _doc(d: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "knowledge_base_id": str(d.knowledge_base_id),
        "title": d.title,
        "source_type": d.source_type,
        "source_uri": d.source_uri,
        "mime_type": d.mime_type,
        "size_bytes": d.size_bytes,
        "status": d.status,
        "error": d.error,
        "chunk_count": d.chunk_count,
        "revision": d.revision,
        "updated_at": d.updated_at.isoformat(),
    }


@router.post("/knowledge-bases", status_code=201)
async def create_kb(
    body: KbIn, ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_WRITE))
) -> dict[str, Any]:
    if await ctx.session.scalar(
        sa.select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.name == body.name
        )
    ):
        raise ConflictError("knowledge base name already used")
    kb = await ctx.services.knowledge.create_kb(
        ctx.session,
        ctx.tenant_id,
        name=body.name,
        description=body.description,
        embedding_provider=body.embedding_provider,
        embedding_model=body.embedding_model,
    )
    return _kb(kb)


@router.get("/knowledge-bases")
async def list_kbs(ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_READ))) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(KnowledgeBase)
            .where(KnowledgeBase.tenant_id == ctx.tenant_id)
            .order_by(KnowledgeBase.name)
        )
    ).all()
    return [_kb(k) for k in rows]


@router.post("/knowledge-bases/{kb_id}/documents", status_code=202)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_WRITE)),
) -> dict[str, Any]:
    limit = ctx.services.settings.max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailedError(f"file exceeds {limit} bytes")
    doc = await ctx.services.knowledge.add_document(
        ctx.session,
        ctx.tenant_id,
        kb_id,
        title=title or file.filename or "document",
        filename=file.filename or "document",
        data=data,
        declared_mime=file.content_type,
        max_bytes=limit,
    )
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="knowledge.upload",
        resource_type="document",
        resource_id=doc.id,
    )
    return _doc(doc)


class UrlIn(BaseModel):
    url: str = Field(pattern=r"^https://")
    title: str | None = None


@router.post("/knowledge-bases/{kb_id}/urls", status_code=202)
async def add_url(
    kb_id: uuid.UUID, body: UrlIn, ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_WRITE))
) -> dict[str, Any]:
    from ...shared.http import EgressPolicy, build_guarded_client, read_limited

    client = build_guarded_client(EgressPolicy(), timeout_s=10)
    try:
        async with client.stream(
            "GET", body.url, headers={"Accept": "text/html,text/plain"}
        ) as resp:
            if resp.status_code != 200:
                raise ValidationFailedError(f"URL returned HTTP {resp.status_code}")
            data = await read_limited(resp, 5 * 1024 * 1024)
            ctype = resp.headers.get("content-type", "text/html")
    finally:
        await client.aclose()
    doc = await ctx.services.knowledge.add_document(
        ctx.session,
        ctx.tenant_id,
        kb_id,
        title=body.title or body.url,
        filename="page.html",
        data=data,
        declared_mime=ctype,
        source_type="url",
        source_uri=body.url,
    )
    return _doc(doc)


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(
    kb_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_READ))
) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == ctx.tenant_id,
                KnowledgeDocument.knowledge_base_id == kb_id,
            )
            .order_by(KnowledgeDocument.created_at)
        )
    ).all()
    return [_doc(d) for d in rows]


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_WRITE))
) -> None:
    await ctx.services.knowledge.delete_document(ctx.session, ctx.tenant_id, doc_id)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="knowledge.delete",
        resource_type="document",
        resource_id=doc_id,
    )


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    knowledge_base_ids: list[uuid.UUID]
    top_k: int = Field(default=4, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"


@router.post("/knowledge/search")
async def search(
    body: SearchIn, ctx: TenantCtx = Depends(tenant_ctx(P.KNOWLEDGE_READ))
) -> list[dict[str, Any]]:
    hits = await ctx.services.knowledge.search(
        ctx.tenant_id, body.knowledge_base_ids, body.query, top_k=body.top_k, mode=body.mode
    )
    return [h.__dict__ for h in hits]


# --- scenarios --------------------------------------------------------------------------------
class ScenarioIn(BaseModel):
    spec: dict[str, Any]
    agent_id: uuid.UUID | None = None


@router.post("/scenarios", status_code=201)
async def create_scenario(
    body: ScenarioIn, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_RUN))
) -> dict[str, Any]:
    try:
        sc = Scenario.model_validate({**body.spec, "source": body.spec.get("source", "manual")})
    except ValidationError as e:
        raise ValidationFailedError(
            "invalid scenario",
            details={
                "errors": [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()]
            },
        ) from e
    row = await ctx.session.scalar(
        sa.select(EvalScenario).where(
            EvalScenario.tenant_id == ctx.tenant_id, EvalScenario.slug == sc.id
        )
    )
    if row is None:
        row = EvalScenario(tenant_id=ctx.tenant_id, slug=sc.id)
        ctx.session.add(row)
    row.name, row.tags, row.agent_id = sc.name, sc.tags, body.agent_id
    row.spec = sc.model_dump(mode="json")
    row.source = sc.source
    await ctx.session.flush()
    return {"id": str(row.id), "slug": row.slug, "name": row.name, "tags": row.tags}


class GenerateIn(BaseModel):
    version_id: uuid.UUID
    count: int = Field(default=5, ge=1, le=20)
    focus: list[str] = Field(default_factory=list, max_length=10)


@router.post("/scenarios/generate", status_code=201)
async def generate(
    body: GenerateIn, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_RUN))
) -> dict[str, Any]:
    """Draft scenarios with the simulator LLM; stored with source=generated for review."""
    from ...config.service import parse_config
    from ...evaluation.generation import generate_scenarios
    from ...providers.types import BuildContext, ProviderKind
    from ..jobs.runner import parse_llm_spec

    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).get_version(body.version_id)
    cfg = parse_config(v.config)
    llm = getattr(ctx.services, "scenario_llm", None) or ctx.services.registry.create(
        ProviderKind.LLM, parse_llm_spec(ctx.services.settings.simulator_llm), BuildContext()
    )
    scenarios, rejected = await generate_scenarios(
        llm, cfg, count=body.count, focus=body.focus or None
    )
    saved = []
    for sc in scenarios:
        row = await ctx.session.scalar(
            sa.select(EvalScenario).where(
                EvalScenario.tenant_id == ctx.tenant_id, EvalScenario.slug == sc.id
            )
        )
        if row is None:
            row = EvalScenario(tenant_id=ctx.tenant_id, slug=sc.id)
            ctx.session.add(row)
        row.name, row.tags, row.agent_id = sc.name, sc.tags, v.agent_id
        row.spec, row.source = sc.model_dump(mode="json"), "generated"
        saved.append(sc.id)
    await ctx.session.flush()
    return {"created": saved, "rejected": rejected}


@router.get("/scenarios")
async def list_scenarios(
    ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ)),
) -> list[dict[str, Any]]:
    from ...evaluation.scenario import load_scenarios

    rows = (
        await ctx.session.scalars(
            sa.select(EvalScenario)
            .where(EvalScenario.tenant_id == ctx.tenant_id)
            .order_by(EvalScenario.slug)
        )
    ).all()
    out: list[dict[str, Any]] = [
        {"slug": r.slug, "name": r.name, "tags": r.tags, "source": r.source} for r in rows
    ]
    try:
        lib = load_scenarios(ctx.services.settings.scenario_library_path + "/receptionist")
        out += [
            {
                "slug": s.id,
                "name": s.name,
                "tags": s.tags,
                "source": "library",
                "critical": s.critical,
            }
            for s in lib
        ]
    except (OSError, ValueError):
        pass
    return out


# --- evaluation runs --------------------------------------------------------------------------
class EvalRunIn(BaseModel):
    agent_id: uuid.UUID
    version_id: uuid.UUID
    suite: str = "receptionist"
    scenario_slugs: list[str] = Field(default_factory=list)
    repetitions: int = Field(default=1, ge=1, le=10)
    seed: int = 0
    use_judge: bool = True


def _run(r: EvalRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "agent_id": str(r.agent_id),
        "agent_version_id": str(r.agent_version_id),
        "suite": r.suite,
        "status": r.status,
        "seed": r.seed,
        "repetitions": r.repetitions,
        "summary": r.summary,
        "error": r.error,
        "created_at": r.created_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.post("/evaluations", status_code=202)
async def start_evaluation(
    body: EvalRunIn, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_RUN))
) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).get_version(body.version_id)
    if v.agent_id != body.agent_id:
        raise ValidationFailedError("version belongs to another agent")
    run = EvalRun(
        tenant_id=ctx.tenant_id,
        agent_id=body.agent_id,
        agent_version_id=v.id,
        suite=body.suite,
        seed=body.seed,
        repetitions=body.repetitions,
        settings={"scenario_slugs": body.scenario_slugs, "use_judge": body.use_judge},
        created_by=ctx.principal.user_id,
    )
    ctx.session.add(run)
    await ctx.session.flush()
    await jobs.enqueue(
        ctx.session,
        kind=EVAL_JOB,
        tenant_id=ctx.tenant_id,
        payload={"run_id": str(run.id)},
        max_attempts=1,
    )
    return _run(run)


@router.get("/evaluations")
async def list_evaluations(
    agent_id: uuid.UUID | None = None, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ))
) -> list[dict[str, Any]]:
    q = sa.select(EvalRun).where(EvalRun.tenant_id == ctx.tenant_id)
    if agent_id:
        q = q.where(EvalRun.agent_id == agent_id)
    rows = (await ctx.session.scalars(q.order_by(EvalRun.created_at.desc()).limit(100))).all()
    return [_run(r) for r in rows]


@router.get("/evaluations/{run_id}")
async def get_evaluation(
    run_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ))
) -> dict[str, Any]:
    run = await ctx.session.scalar(
        sa.select(EvalRun).where(EvalRun.id == run_id, EvalRun.tenant_id == ctx.tenant_id)
    )
    if run is None:
        raise NotFoundError("evaluation not found")
    results = (
        await ctx.session.scalars(
            sa.select(EvalResult)
            .where(EvalResult.run_id == run_id)
            .order_by(EvalResult.scenario_slug, EvalResult.repetition)
        )
    ).all()
    return {
        **_run(run),
        "results": [
            {
                "scenario": r.scenario_slug,
                "repetition": r.repetition,
                "passed": r.passed,
                "score": r.score,
                "category_scores": r.category_scores,
                "failures": r.failures,
                "metrics": r.metrics,
                "judge": r.judge,
                "transcript": r.transcript,
                "tool_calls": r.tool_calls,
            }
            for r in results
        ],
    }


@router.get("/evaluations/{baseline_id}/compare/{candidate_id}")
async def compare_runs(
    baseline_id: uuid.UUID,
    candidate_id: uuid.UUID,
    ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ)),
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, rid in (("baseline", baseline_id), ("candidate", candidate_id)):
        run = await ctx.session.scalar(
            sa.select(EvalRun).where(EvalRun.id == rid, EvalRun.tenant_id == ctx.tenant_id)
        )
        if run is None or run.status != "succeeded":
            raise NotFoundError(f"{label} run not found or not finished")
        out[label] = run.summary
    b, c = (
        out["baseline"].get("scenario_pass_rates", {}),
        out["candidate"].get("scenario_pass_rates", {}),
    )
    out["regressions"] = sorted(s for s in b if c.get(s, 0) < b[s])
    out["improvements"] = sorted(s for s in c if c[s] > b.get(s, 0))
    out["delta_pass_rate"] = round(
        out["candidate"].get("pass_rate", 0) - out["baseline"].get("pass_rate", 0), 4
    )
    return out


# --- optimization -----------------------------------------------------------------------------
class OptimizeIn(BaseModel):
    agent_id: uuid.UUID
    baseline_version_id: uuid.UUID
    scenario_slugs: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=3, ge=1, le=10)
    repetitions: int = Field(default=1, ge=1, le=5)
    seed: int = 0
    min_pass_rate: float = Field(default=0.8, ge=0, le=1)
    allowed_models: dict[str, list[str]] = Field(default_factory=dict)


def _opt(o: OptimizationRun) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "agent_id": str(o.agent_id),
        "baseline_version_id": str(o.baseline_version_id),
        "status": o.status,
        "decision": o.decision,
        "best_candidate_version_id": str(o.best_candidate_version_id)
        if o.best_candidate_version_id
        else None,
        "report": o.report,
        "report_markdown": o.report_markdown,
        "error": o.error,
        "created_at": o.created_at.isoformat(),
        "approved_by": str(o.approved_by) if o.approved_by else None,
    }


@router.post("/optimizations", status_code=202)
async def start_optimization(
    body: OptimizeIn, ctx: TenantCtx = Depends(tenant_ctx(P.OPTIMIZATIONS_RUN))
) -> dict[str, Any]:
    v = await AgentService(ctx.session, ctx.tenant_id, ctx.principal).get_version(
        body.baseline_version_id
    )
    if v.agent_id != body.agent_id:
        raise ValidationFailedError("version belongs to another agent")
    run = OptimizationRun(
        tenant_id=ctx.tenant_id,
        agent_id=body.agent_id,
        baseline_version_id=v.id,
        created_by=ctx.principal.user_id,
        settings=body.model_dump(mode="json"),
    )
    ctx.session.add(run)
    await ctx.session.flush()
    await jobs.enqueue(
        ctx.session,
        kind=OPTIMIZE_JOB,
        tenant_id=ctx.tenant_id,
        payload={"optimization_run_id": str(run.id)},
        max_attempts=1,
    )
    return _opt(run)


@router.get("/optimizations")
async def list_optimizations(
    ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ)),
) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.scalars(
            sa.select(OptimizationRun)
            .where(OptimizationRun.tenant_id == ctx.tenant_id)
            .order_by(OptimizationRun.created_at.desc())
            .limit(50)
        )
    ).all()
    return [_opt(o) for o in rows]


@router.get("/optimizations/{opt_id}")
async def get_optimization(
    opt_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.EVALUATIONS_READ))
) -> dict[str, Any]:
    o = await ctx.session.scalar(
        sa.select(OptimizationRun).where(
            OptimizationRun.id == opt_id, OptimizationRun.tenant_id == ctx.tenant_id
        )
    )
    if o is None:
        raise NotFoundError("optimization run not found")
    return _opt(o)


class ApproveIn(BaseModel):
    environment: Literal["dev", "staging", "prod"] = "staging"
    canary_percent: float = Field(default=10, ge=0, le=100)
    notes: str = ""


@router.post("/optimizations/{opt_id}/approve")
async def approve_optimization(
    opt_id: uuid.UUID,
    body: ApproveIn,
    ctx: TenantCtx = Depends(tenant_ctx(P.OPTIMIZATIONS_APPROVE)),
) -> dict[str, Any]:
    """Human approval: publish the eligible candidate and roll it out as a canary (or full
    deploy if there is no stable version yet)."""
    o = await ctx.session.scalar(
        sa.select(OptimizationRun)
        .where(OptimizationRun.id == opt_id, OptimizationRun.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if o is None:
        raise NotFoundError("optimization run not found")
    if o.status != "succeeded" or o.decision != "eligible" or not o.best_candidate_version_id:
        raise ConflictError("only eligible, finished optimizations can be approved")
    if o.approved_by is not None:
        raise ConflictError("already approved")
    svc = AgentService(ctx.session, ctx.tenant_id, ctx.principal)
    cand = await svc.publish(o.best_candidate_version_id)
    dep = await svc.get_deployment(o.agent_id, body.environment)
    if dep is None or body.canary_percent >= 100:
        dep = await svc.deploy(o.agent_id, body.environment, cand.id)
    else:
        dep = await svc.set_canary(o.agent_id, body.environment, cand.id, body.canary_percent)
    o.approved_by = ctx.principal.user_id
    o.decision = "approved"
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="optimization.approve",
        resource_type="optimization_run",
        resource_id=o.id,
        details={
            "environment": body.environment,
            "canary_percent": body.canary_percent,
            "version": cand.version,
        },
    )
    return {
        "optimization": _opt(o),
        "version": cand.version,
        "deployment": {
            "environment": dep.environment,
            "version_id": str(dep.version_id),
            "canary_version_id": str(dep.canary_version_id) if dep.canary_version_id else None,
            "canary_percent": dep.canary_percent,
        },
    }


@router.post("/optimizations/{opt_id}/reject")
async def reject_optimization(
    opt_id: uuid.UUID, ctx: TenantCtx = Depends(tenant_ctx(P.OPTIMIZATIONS_APPROVE))
) -> dict[str, Any]:
    o = await ctx.session.scalar(
        sa.select(OptimizationRun).where(
            OptimizationRun.id == opt_id, OptimizationRun.tenant_id == ctx.tenant_id
        )
    )
    if o is None:
        raise NotFoundError("optimization run not found")
    if o.best_candidate_version_id:
        v = await ctx.session.get(AgentVersion, o.best_candidate_version_id)
        if v is not None and v.status == "candidate":
            v.status = "rejected"
    o.decision = "rejected"
    o.finished_at = o.finished_at or datetime.now(UTC)
    await record_audit(
        ctx.session,
        tenant_id=ctx.tenant_id,
        actor=ctx.principal,
        action="optimization.reject",
        resource_type="optimization_run",
        resource_id=o.id,
    )
    return _opt(o)
