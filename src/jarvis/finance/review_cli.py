"""Run a local review workspace without production configuration or services."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

from jarvis.finance.review_store import ReviewStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--packet", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--agent-journal", type=Path, help="Existing guarded private staging agent journal")
    serve.add_argument("--insight-policy", type=Path, help="Local bounded InsightPolicy JSON; no permissions")
    serve.add_argument("--public-research-mode", choices=("synthetic", "owner-http"), help="Bind existing public evidence for task result freshness")
    serve.add_argument("--ledger-snapshot", type=Path)
    serve.add_argument("--accepted-source", action="append", default=[])
    serve.add_argument("--accepted-sha256", help="Explicitly accepted immutable ledger SHA-256")
    serve.add_argument("--knowledge-index", type=Path)
    serve.add_argument("--public-knowledge-source", action="append", default=[])
    serve.add_argument("--ai-budget-journal", type=Path)
    serve.add_argument("--ai-prices", type=Path)
    sub.add_parser("status")
    agent_init = sub.add_parser("agent-init")
    agent_init.add_argument("--journal", type=Path, required=True)
    sub.add_parser("plan")
    sources = sub.add_parser("attach-sources")
    sources.add_argument("--statements", type=Path, required=True)
    labels = sub.add_parser("labels-init")
    labels.add_argument("--policy", type=Path, required=True)
    sub.add_parser("labels-export")
    annotation = sub.add_parser('annotation-export')
    annotation.add_argument('--output', type=Path, required=True)
    annotation.add_argument('--private-local', action='store_true')
    context_preview = sub.add_parser('annotation-context-preview')
    context_preview.add_argument('--output', type=Path, required=True)
    context_preview.add_argument('--private-local', action='store_true')
    context_export = sub.add_parser('annotation-context-export')
    context_export.add_argument('--review', type=Path, required=True)
    context_export.add_argument('--output', type=Path, required=True)
    annotation_import = sub.add_parser('annotation-import')
    annotation_import.add_argument('--file', type=Path, required=True)
    annotation_import.add_argument('--actor', required=True)
    annotation_import.add_argument('--confirm-proposals-only', action='store_true')
    annotation_import.add_argument('--context-review', type=Path)
    sub.add_parser("report")
    sub.add_parser("availability")
    sub.add_parser("provenance")
    sub.add_parser("backup")
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    engine_export = sub.add_parser("engine-export")
    engine_export.add_argument("--output", type=Path, required=True, help="New operator-private bundle directory")
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    rehearsal = sub.add_parser("rehearse")
    rehearsal.add_argument("--snapshot", type=Path, required=True)
    rehearsal.add_argument("--statements", type=Path, required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--batch", required=True)
    question = sub.add_parser("question")
    question.add_argument("--database", type=Path, required=True)
    question.add_argument("--month", required=True)
    question.add_argument("--currency", default="RUB")
    question.add_argument("--intent", required=True, choices=["spending", "expense_history", "category_growth", "recurring", "duplicates", "missing_data", "stale_balances", "available_funds"])
    question.add_argument("--accepted-source", action="append", default=[])
    question.add_argument("--accepted-sha256", help="Explicitly accepted immutable ledger SHA-256")
    args = parser.parse_args()
    store = ReviewStore(args.workspace)
    if args.command == "agent-init":
        from jarvis.finance.db import FinanceDB
        if args.journal.exists() or args.journal.is_symlink() or any(p.is_symlink() for p in args.journal.parents):
            parser.error("New explicit nonsymlink private journal required")
        store.items()
        args.journal.parent.mkdir(parents=True, exist_ok=True)
        with args.journal.open("xb"):
            pass
        journal = FinanceDB(args.journal)
        journal.init()
        journal.set_setting("agent_runtime_scope", "private_staging")
        print(json.dumps({"status": "private_agent_journal_created", "production_database_modified": False}))
    elif args.command == "question":
        from jarvis.finance.audit_report import financial_question
        print(json.dumps(financial_question(args.database, month=args.month, intent=args.intent,
            currency=args.currency, accepted_sources=frozenset(args.accepted_source),
            accepted_sha256=args.accepted_sha256), ensure_ascii=False, indent=2))
    elif args.command == "engine-export":
        from jarvis.finance.review_engine_export import (
            ExportUnavailable,
            write_engine_bundle,
        )
        try:
            print(json.dumps(write_engine_bundle(store, args.output)))
        except ExportUnavailable:
            print(json.dumps({"status": "unavailable", "code": "export_unavailable", "apply_allowed": False}))
            return 2
    elif args.command == "provenance":
        from jarvis.finance.review_provenance import provenance_matrix
        print(json.dumps(provenance_matrix(store), ensure_ascii=False, indent=2))
    elif args.command == "availability":
        from jarvis.finance.review_availability import source_availability
        print(json.dumps(source_availability(store), ensure_ascii=False, indent=2))
    elif args.command in {'annotation-context-preview', 'annotation-context-export'}:
        from jarvis.finance.classification.annotation import decode_import
        from jarvis.finance.classification.annotation_context import (
            AnnotationContext,
            ContextExportRequest,
        )
        context = AnnotationContext(store)
        try:
            if args.command == 'annotation-context-preview':
                result = context.preview(private=args.private_local)
            else:
                with args.review.open('rb') as source:
                    raw = source.read(2 * 1024 * 1024 + 1)
                result = context.export_reviewed(ContextExportRequest.model_validate(decode_import(raw)))
            with args.output.open('x', encoding='utf-8') as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({'coverage': result['coverage'], 'database_modified': False, 'upload_authorized': False}))
        except (ValueError, KeyError, OSError):
            print(json.dumps({'status': 'refused', 'reason': 'Invalid/stale context review; no records changed'}))
            return 2
    elif args.command in {'annotation-export', 'annotation-import'}:
        from jarvis.finance.classification.annotation import (
            AnnotationExchange,
            decode_import,
        )
        exchange = AnnotationExchange(store)
        if args.command == 'annotation-export':
            result = exchange.export(private=args.private_local)
            with args.output.open('x', encoding='utf-8') as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({'observations': result['observations'], 'variant': result['variant'],
                              'database_modified': False, 'upload_authorized': False}))
        else:
            with args.file.open('rb') as source:
                raw = source.read(2 * 1024 * 1024 + 1)
            try:
                context_review = None
                if args.context_review:
                    with args.context_review.open('rb') as source:
                        context_review = decode_import(source.read(2 * 1024 * 1024 + 1))
                result = exchange.import_proposals(decode_import(raw), actor=args.actor,
                                                  confirmed=args.confirm_proposals_only, context_review=context_review)
            except (ValueError, KeyError, OSError):
                print(json.dumps({'status': 'refused', 'reason': 'Invalid/stale annotation; no labels written'}))
                return 2
            print(json.dumps(result))
    elif args.command == "export":
        from jarvis.finance.review_export import build_review_export
        report = build_review_export(store)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"output": str(args.output), "payload_sha256": report["payload_sha256"],
                          "database_modified": False}))
    elif args.command in {"backup", "restore"}:
        from jarvis.finance.review_backup import (
            create_review_backup,
            restore_review_backup,
        )
        result = (create_review_backup(store) if args.command == "backup"
                  else restore_review_backup(args.archive, args.workspace))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "init":
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        result = store.append_native_capture(packet) if packet.get("source_kind") == "sber_mcp" else store.initialize(packet)
        print(json.dumps(result, indent=2))
    elif args.command == "status":
        events = store.audit()
        print(json.dumps({"items": len(store.items()),
                          "human_decisions": sum(e.get("domain") not in {"native_capture", "memory_note"} for e in events),
                          "capture_events": sum(e.get("domain") == "native_capture" for e in events),
                          "memory_events": sum(e.get("domain") == "memory_note" for e in events),
                          "production_database_modified": False}, indent=2))
    elif args.command in {"labels-init", "labels-export"}:
        from jarvis.finance.review_labels import HumanLabels
        human = HumanLabels(store)
        if args.command == "labels-init":
            human.configure(json.loads(args.policy.read_text(encoding="utf-8")))
        print(json.dumps(human.export(), ensure_ascii=False, indent=2))
    elif args.command in {"attach-sources", "report"}:
        from jarvis.finance.review_sources import attach_sources, workspace_report
        report = attach_sources(store, args.statements) if args.command == "attach-sources" else workspace_report(store)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command in {"plan", "rehearse", "rollback"}:
        from jarvis.finance.review_staging import (
            build_staging_plan,
            rehearse,
            rollback_rehearsal,
        )
        if args.command == "plan":
            result = build_staging_plan(store)
        elif args.command == "rehearse":
            result = rehearse(store, args.snapshot, args.statements)
        else:
            result = rollback_rehearsal(store, args.batch)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn

        from jarvis.finance.review_ui import create_review_app
        knowledge = None
        if args.knowledge_index:
            from jarvis.knowledge.retriever import KnowledgeRetriever
            from jarvis.knowledge.store import KnowledgeStore
            if not args.knowledge_index.is_dir():
                parser.error("Existing approved public knowledge index required")
            knowledge = KnowledgeRetriever(KnowledgeStore(args.knowledge_index))
        store.items()
        agent = None
        if args.public_research_mode and not args.agent_journal:
            parser.error("Public research task panel requires an explicit agent journal")
        if args.agent_journal:
            from jarvis.finance.db import FinanceDB
            from jarvis.finance.scheduler.agent import AgentRuntime
            if args.public_research_mode:
                from jarvis.finance.scheduler.research_demo import research_runtime
                if args.agent_journal.resolve() != store.workspace.parent.resolve() / "agent.sqlite":
                    parser.error("Public research journal/workspace identity mismatch")
                agent = research_runtime(store.workspace.parent, owner_public_http=args.public_research_mode == "owner-http")
            else:
                from jarvis.finance.insights.deterministic import InsightPolicy
                from jarvis.services.assistant import AssistantService
                try:
                    policy = InsightPolicy.model_validate_json(args.insight_policy.read_text(encoding="utf-8")) if args.insight_policy else None
                except (OSError, ValueError):
                    parser.error("Invalid bounded local insight policy; no raw input output")
                agent = AgentRuntime(FinanceDB(args.agent_journal), store, insight_policy=policy,
                    assistant=AssistantService(store, ledger=args.ledger_snapshot,
                        accepted_sources=frozenset(args.accepted_source), accepted_sha256=args.accepted_sha256))
        from jarvis.finance.review_auth import publish_owner_key
        owner_key = secrets.token_urlsafe(32)
        publish_owner_key(store.workspace, owner_key)
        uvicorn.run(create_review_app(store, owner_key,
                    ledger_path=args.ledger_snapshot, accepted_sources=frozenset(args.accepted_source),
                    accepted_sha256=args.accepted_sha256, knowledge_retriever=knowledge,
                    knowledge_sources=frozenset(args.public_knowledge_source), ai_budget_path=args.ai_budget_journal,
                    ai_pricing_path=args.ai_prices, agent_runtime=agent),
                    host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
