"""Operational CLI: ``vp migrate | bootstrap | jobs | eval | rotate-keys | gen-key``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import sqlalchemy as sa


def _migrate(args: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, args.revision)


async def _bootstrap(args: argparse.Namespace) -> None:
    from ..core.db import DbScope
    from ..core.models import User
    from ..shared.crypto import hash_password
    from .services import Services

    services = Services.from_settings()
    try:
        async with services.db.session(DbScope.system()) as s:
            existing = await s.scalar(sa.select(User).where(User.email == args.email.lower()))
            if existing is not None:
                existing.is_platform_admin = True
                print("existing user promoted to platform admin")
            else:
                s.add(User(email=args.email.lower(), password_hash=hash_password(args.password),
                           full_name=args.name, is_platform_admin=True))
                print("platform admin created")
    finally:
        await services.aclose()


async def _rotate(args: argparse.Namespace) -> None:
    from ..core.db import DbScope
    from .services import Services

    services = Services.from_settings()
    try:
        async with services.db.session(DbScope.system()) as s:
            n = await services.vault.rotate_all(s)
        print(f"re-encrypted {n} credential(s) with the primary key")
    finally:
        await services.aclose()


async def _eval(args: argparse.Namespace) -> int:
    """Run a scenario suite locally against a config file using real providers."""
    import yaml

    from ..config.service import parse_config
    from ..evaluation.evaluators import LLMJudge
    from ..evaluation.runner import BenchmarkRunner
    from ..evaluation.scenario import load_scenarios
    from ..providers.registry import default_registry
    from ..providers.types import BuildContext, ProviderKind
    from .jobs.runner import parse_llm_spec

    cfg = parse_config(yaml.safe_load(Path(args.config).read_text())).materialize(args.env)
    reg = default_registry()
    assert cfg.pipeline.llm is not None

    def make(spec_ref: object) -> object:
        return reg.create(ProviderKind.LLM, spec_ref, BuildContext())  # type: ignore[arg-type]

    judge = LLMJudge(make(parse_llm_spec(args.judge)), model_label=args.judge) if args.judge \
        else None
    runner = BenchmarkRunner(agent_llm_factory=lambda c: make(c.pipeline.llm.chain()[0]),  # type: ignore[union-attr]
                             caller_llm=make(parse_llm_spec(args.caller)), judge=judge,
                             concurrency=args.concurrency)
    scenarios = load_scenarios(args.scenarios)
    if args.only:
        scenarios = [s for s in scenarios if s.id in set(args.only.split(","))]
    result = await runner.run(cfg, scenarios, repetitions=args.repetitions, seed=args.seed)
    summary = result.summary()
    print(json.dumps(summary, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps([r.as_row() for r in result.records], indent=2,
                                             default=str))
    return 0 if summary["pass_rate"] >= args.min_pass_rate else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vp")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply database migrations")
    m.add_argument("revision", nargs="?", default="head")
    b = sub.add_parser("bootstrap", help="create or promote a platform administrator")
    b.add_argument("--email", required=True)
    b.add_argument("--password", required=True)
    b.add_argument("--name", default="Platform Admin")
    sub.add_parser("jobs", help="run the background job runner")
    sub.add_parser("rotate-keys", help="re-encrypt credentials with the primary key")
    sub.add_parser("gen-key", help="print a new encryption key")
    e = sub.add_parser("eval", help="run scenarios locally against a config file")
    e.add_argument("--config", required=True)
    e.add_argument("--scenarios", default="scenarios/receptionist")
    e.add_argument("--env", default="staging")
    e.add_argument("--only", default="")
    e.add_argument("--caller", default="openai:gpt-4.1-mini")
    e.add_argument("--judge", default="")
    e.add_argument("--repetitions", type=int, default=1)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--concurrency", type=int, default=4)
    e.add_argument("--min-pass-rate", type=float, default=0.0)
    e.add_argument("--out", default="")
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        _migrate(args)
    elif args.cmd == "bootstrap":
        asyncio.run(_bootstrap(args))
    elif args.cmd == "jobs":
        from .jobs.runner import main as jobs_main

        asyncio.run(jobs_main())
    elif args.cmd == "rotate-keys":
        asyncio.run(_rotate(args))
    elif args.cmd == "gen-key":
        from ..shared.crypto import generate_fernet_key

        print(generate_fernet_key())
    elif args.cmd == "eval":
        return asyncio.run(_eval(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
