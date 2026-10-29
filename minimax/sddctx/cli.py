"""命令行入口：``python -m sddctx <command>``。

命令一览（完整说明见 minimax/README.md）：

    doctor                       环境自检：数据库、PowerContext、MiniMax Key、依赖
    init [--reset]               建库建表
    ingest [--variant v1|v2]     导入规格 / 代码 / 测试并重建索引
    context --task TASK-0007     组装开发上下文（--json 输出机器可读版本）
    check --task TASK-0007       校验上下文是否包含任务要求的全部来源
    verify --task TASK-0007      运行目标测试与回归测试并记录状态
    state show|step-done|reset   查看 / 推进 / 清空任务状态（PowerContext）
    codegen --task TASK-0007     调用 MiniMax 生成代码修改（--apply 落地为快照）
    models                       列出当前 Key 可用的 MiniMax 模型（核对模型名）
    demo reset|apply|sample      复现「修改前失败 / 修改后通过」与整条验证样例
    serve                        启动本地 HTTP 接口
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sddctx import __version__
from sddctx.config import load_settings
from sddctx.context import TaskNotFound, build_context, load_task
from sddctx.db import Database, DatabaseError, open_database
from sddctx.demo import apply_snapshot, reset_project, run_sample
from sddctx.ingest import ingest
from sddctx.state import open_state, task_state_view
from sddctx.verify import verify_task


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _open(args: argparse.Namespace):
    settings = load_settings()
    db = open_database(settings)
    store, reason = open_state(settings)
    return settings, db, store, reason


# ---------------------------------------------------------------------------- 命令
def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    info: dict[str, Any] = {
        "sddctx_version": __version__,
        "python": sys.version.split()[0],
        "root": str(settings.root),
        "data_dir": str(settings.data_dir),
        "env_file": str(settings.root / ".env.local"),
        "minimax": {
            "model": settings.minimax_model,
            "base_url": settings.minimax_base_url,
            "api_key_present": bool(settings.minimax_api_key),
        },
    }
    try:
        db = open_database(settings)
        info["database"] = db.describe()
        db.close()
    except DatabaseError as exc:
        info["database"] = {"error": str(exc)}
    store, reason = open_state(settings)
    info["state"] = {"backend": store.backend, "scope": store.scope_label, "fallback_reason": reason}
    try:
        import pymysql  # noqa: F401

        info["pymysql"] = "installed"
    except ImportError:
        info["pymysql"] = "missing"
    try:
        import pytest  # noqa: F401

        info["pytest"] = getattr(pytest, "__version__", "installed")
    except ImportError:
        info["pytest"] = "missing"
    if args.json:
        _print_json(info)
        return 0
    print(f"sddctx {info['sddctx_version']}  |  Python {info['python']}")
    print(f"工作目录：{info['root']}")
    print(f"数据目录：{info['data_dir']}")
    database = info["database"]
    if "error" in database:
        print(f"数据库：不可用（{database['error']}）")
    else:
        tables = ", ".join(f"{name}={count}" for name, count in database["tables"].items())
        print(f"数据库：{database['backend']} / {database['label']} / 库 {database['database']}")
        print(f"  表行数：{tables}")
        if database.get("fallback_reason"):
            print(f"  降级原因：{database['fallback_reason']}")
    state = info["state"]
    print(f"状态后端：{state['backend']} / {state['scope']}")
    if state.get("fallback_reason"):
        print(f"  降级原因：{state['fallback_reason']}")
    print(
        f"MiniMax：model={info['minimax']['model']}  key={'已配置' if info['minimax']['api_key_present'] else '未配置'}"
    )
    print(f"依赖：pymysql={info['pymysql']}  pytest={info['pytest']}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = open_database(settings)
    db.ensure_schema(reset=args.reset)
    print(f"初始化完成：{db.backend} / {db.label} / 库 {db.database}")
    for name, count in db.table_counts().items():
        print(f"  {name}: {count}")
    db.close()
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = load_settings()
    db = open_database(settings)
    report = ingest(settings, db, spec_variant=args.variant, reset=args.reset)
    total_chunks = db.scalar("SELECT COUNT(*) FROM pc_chunks")
    total_postings = db.scalar("SELECT COUNT(*) FROM pc_postings")
    print(
        f"导入完成（{report.backend}）：扫描 {report.documents} 个文档"
        f"（重建 {len(report.changed)} / 跳过 {report.unchanged}），"
        f"本次生成 {report.chunks} 个块 / {report.postings} 条倒排"
    )
    print(
        f"库内现有：{total_chunks} 个块 / {total_postings} 条倒排 / {report.terms} 个检索词"
    )
    print(f"规格版本：{report.spec_variant}   耗时：{report.elapsed_ms} ms")
    for category, count in sorted(report.categories.items()):
        print(f"  {category}: {count} 个文档")
    for path in report.files:
        print(f"  - {path}")
    print(
        "SQL 抽样："
        f"INSERT INTO pc_postings ...（{report.postings} 行）、"
        "INSERT INTO pc_df SELECT term, COUNT(*) FROM pc_postings GROUP BY term"
    )
    db.close()
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    settings, db, store, reason = _open(args)
    task = load_task(settings, args.task)
    pack = build_context(settings, db, store, task, state_fallback_reason=reason, persist=not args.no_persist)
    if args.json:
        _print_json(pack.payload)
    else:
        print(pack.markdown)
    if not args.no_persist:
        print(f"\n上下文已保存：{pack.payload.get('run_dir')}", file=sys.stderr)
    db.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings, db, store, reason = _open(args)
    task = load_task(settings, args.task)
    pack = build_context(settings, db, store, task, state_fallback_reason=reason, persist=False)
    checks = pack.payload["checks"]
    ok = bool(checks["all_required_present"])
    if args.variant and pack.payload["spec_variant"] != args.variant:
        ok = False
    if args.json:
        _print_json({"ok": ok, "checks": checks, "spec_variant": pack.payload["spec_variant"]})
    else:
        print(f"任务：{task['task_id']}  规格版本：{pack.payload['spec_variant']}")
        print(f"必需来源齐全：{'是' if checks['all_required_present'] else '否'}")
        for category in ("spec", "code", "test"):
            required = checks["required"].get(category, [])
            if not required:
                continue
            present = checks["present"].get(category, [])
            print(f"  {category}:")
            for source_id in required:
                mark = "✓" if source_id in present else "✗"
                print(f"    {mark} {source_id}")
        for marker in checks.get("markers", []):
            print(f"  标记「{marker['text']}」：{'命中' if marker['found'] else '未命中'}（{marker['actual_source'] or '—'}）")
        if args.variant:
            print(f"  期望规格版本 {args.variant}，实际 {pack.payload['spec_variant']}")
        print(f"结论：{'通过' if ok else '不通过'}")
    db.close()
    return 0 if ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    settings, db, store, _reason = _open(args)
    task = load_task(settings, args.task)
    result = verify_task(settings, db, store, task, phase=args.phase)
    if args.json:
        _print_json(result)
    else:
        print(f"任务：{task['task_id']}  阶段：{result['phase']}  代码版本：{result['commit'][:12] or 'n/a'}")
        print(f"目标测试：exit={result['target']['exit_code']}  {'通过' if result['target']['passed'] else '失败'}")
        for line in result["target"].get("output_tail", []):
            print(f"    {line}")
        print(f"回归测试：exit={result['regression']['exit_code']}  {'通过' if result['regression']['passed'] else '失败'}")
        for line in result["regression"].get("output_tail", []):
            print(f"    {line}")
        print(f"结论：{result['summary']}")
    db.close()
    return 0 if result["all_passed"] else 1


def cmd_state(args: argparse.Namespace) -> int:
    settings = load_settings()
    store, reason = open_state(settings)
    if args.action == "reset":
        store.reset()
        print(f"状态已清空（后端 {store.backend}）")
        return 0
    if args.action == "step-done":
        store.append(args.task, "step_done", step=args.step)
        print(f"已记录步骤完成：{args.step}")
        return 0
    task = load_task(settings, args.task)
    view = task_state_view(store, task, reason)
    if args.json:
        _print_json(view)
    else:
        print(f"状态后端：{view['backend']} / {view['scope']}")
        if view["fallback_reason"]:
            print(f"降级原因：{view['fallback_reason']}")
        print(f"事件数：{view['event_count']}")
        print("已完成：")
        for item in view["completed"] or ["（无）"]:
            print(f"  - {item}")
        print("待办：")
        for item in view["pending"] or ["（无）"]:
            print(f"  - {item}")
        print("验证记录：")
        for item in view["verifications"] or [{"phase": "（无）"}]:
            print(f"  - {item}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    from sddctx.minimax_client import MiniMaxError, list_models

    settings = load_settings()
    print(f"接口：{settings.minimax_base_url}/v1/models  key：{'已配置' if settings.minimax_api_key else '未配置'}")
    try:
        names = list_models(settings)
    except MiniMaxError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 4
    for name in names:
        mark = "  ← 当前使用" if name == settings.minimax_model else ""
        print(f"  - {name}{mark}")
    return 0


def cmd_codegen(args: argparse.Namespace) -> int:
    from sddctx.codegen import generate

    settings, db, store, reason = _open(args)
    task = load_task(settings, args.task)
    # 先刷新索引：代码改过之后如果直接检索，命中的会是上一版文件，
    # 模型就会照着旧实现（甚至已经改好的实现）写 diff。增量导入只重建变更文档。
    report = ingest(settings, db, spec_variant=settings.spec_variant)
    print(
        f"索引已同步：{report.documents} 个文档（重建 {len(report.changed)} / 跳过 {report.unchanged}），"
        f"耗时 {report.elapsed_ms} ms",
        file=sys.stderr,
    )
    pack = build_context(settings, db, store, task, state_fallback_reason=reason, persist=True)
    report = generate(
        settings,
        pack,
        task_id=task["task_id"],
        model=args.model,
        dry_run=args.dry_run,
        apply=args.apply,
        temperature=args.temperature,
        extra_instruction=args.instruction,
    )
    _print_json(report)
    db.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    settings, db, store, reason = _open(args)
    if args.action == "reset":
        files = reset_project(settings)
        print("已恢复到修改前：" + ", ".join(files))
        print("下一步：python -m sddctx verify --task TASK-0007 --phase before（预期目标测试失败）")
        return 0
    if args.action == "apply":
        files = apply_snapshot(settings)
        store.append("TASK-0007", "patch_applied", files=files, patch="patches/TASK-0007.patch")
        print("已应用修改后快照：" + ", ".join(files))
        print("下一步：python -m sddctx verify --task TASK-0007 --phase after（预期全部通过）")
        return 0
    result = run_sample(settings, db, store, args.task)
    db.close()
    return 0 if result["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from sddctx.http import serve

    serve(load_settings(), host=args.host, port=args.port)
    return 0


# ---------------------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sddctx", description="SDD 上下文管理模块")
    parser.add_argument("--version", action="version", version=f"sddctx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="建库建表")
    init.add_argument("--reset", action="store_true")
    init.set_defaults(func=cmd_init)

    ing = sub.add_parser("ingest", help="导入工程资料")
    ing.add_argument("--variant", default="v1", help="规格版本：v1 / v2")
    ing.add_argument("--reset", action="store_true", help="清空后全量重建")
    ing.set_defaults(func=cmd_ingest)

    ctx = sub.add_parser("context", help="组装开发上下文")
    ctx.add_argument("--task", default="TASK-0007")
    ctx.add_argument("--json", action="store_true")
    ctx.add_argument("--no-persist", action="store_true")
    ctx.set_defaults(func=cmd_context)

    chk = sub.add_parser("check", help="校验上下文必需来源")
    chk.add_argument("--task", default="TASK-0007")
    chk.add_argument("--variant", default="", help="期望的规格版本")
    chk.add_argument("--json", action="store_true")
    chk.set_defaults(func=cmd_check)

    ver = sub.add_parser("verify", help="运行测试并记录状态")
    ver.add_argument("--task", default="TASK-0007")
    ver.add_argument("--phase", default="manual", choices=["before", "after", "manual"])
    ver.add_argument("--json", action="store_true")
    ver.set_defaults(func=cmd_verify)

    state = sub.add_parser("state", help="任务状态（PowerContext）")
    state.add_argument("action", choices=["show", "step-done", "reset"])
    state.add_argument("--task", default="TASK-0007")
    state.add_argument("--step", default="S1")
    state.add_argument("--json", action="store_true")
    state.set_defaults(func=cmd_state)

    models_cmd = sub.add_parser("models", help="列出可用 MiniMax 模型")
    models_cmd.set_defaults(func=cmd_models)

    code = sub.add_parser("codegen", help="调用 MiniMax 生成代码修改")
    code.add_argument("--task", default="TASK-0007")
    code.add_argument("--model", default=None)
    code.add_argument("--temperature", type=float, default=0.1)
    code.add_argument("--dry-run", action="store_true", help="只落盘请求，不调用模型")
    code.add_argument("--apply", action="store_true", help="把 diff 应用并生成快照")
    code.add_argument("--instruction", default="", help="补充给模型的额外要求")
    code.set_defaults(func=cmd_codegen)

    demo = sub.add_parser("demo", help="验证样例编排")
    demo.add_argument("action", choices=["reset", "apply", "sample"])
    demo.add_argument("--task", default="TASK-0007")
    demo.set_defaults(func=cmd_demo)

    serve_cmd = sub.add_parser("serve", help="启动本地 HTTP 接口")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    from sddctx.minimax_client import MiniMaxError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TaskNotFound as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except DatabaseError as exc:
        print(f"[错误] 数据库不可用：{exc}", file=sys.stderr)
        print("提示：检查 SEEKDB_HOST/SEEKDB_PORT，或设置 SDDCTX_DB=sqlite 使用离线降级。", file=sys.stderr)
        return 3
    except MiniMaxError as exc:
        print(f"[错误] MiniMax 调用失败：{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
