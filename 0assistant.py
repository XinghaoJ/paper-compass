"""命令行入口：统一调度检索、Zotero 下载、知识库建立和跨文献问答。"""
import argparse
import sys
from importlib import import_module
from pathlib import Path


def build_parser():
    """命令行参数配置器：定义筛选、下载、知识库建库和问答命令。"""
    parser = argparse.ArgumentParser(description="自动文献阅读助手：筛选、下载与知识库问答")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="按 user_settings.py 检索、筛选摘要并获取 DOI")
    search.add_argument("--query", help="覆盖配置中的检索关键词")
    search.add_argument("--question", "--topic", dest="question", help="覆盖配置中的关注问题")
    search.add_argument("--year-start", type=int)
    search.add_argument("--year-end", type=int)
    search.add_argument("--max-papers", type=int)
    search.add_argument("--output", type=Path)
    search.add_argument("--model")
    search.add_argument("--download", action="store_true", help="筛选结束后自动调用 Zotero 获取全文")
    download = commands.add_parser("download", help="读取筛选清单或 DOI 文本，自动通过 Zotero 获取全文")
    download.add_argument("--input", type=Path, help="默认读取当前研究配置对应的 .papers.json")
    download.add_argument("--output", type=Path, help="默认保存到 DOWNLOAD_SETTINGS.output_dir")
    download.add_argument("--limit", type=int)
    download.add_argument("--dry-run", action="store_true", help="只列出任务，不连接 Zotero 或下载")
    commands.add_parser("zotero-check", help="检查本机 Zotero 插件连接与配对")
    index = commands.add_parser("index", help="第四步：按页解析 PDF，增量建立本地向量知识库")
    index.add_argument("--input", type=Path, help="默认沿用第二步下载目录")
    index.add_argument("--database", type=Path, help="知识库 SQLite 文件路径")
    index.add_argument("--limit", type=int, help="本次最多尝试建立索引的文献数，已完成文献不占名额")
    index.add_argument("--dry-run", action="store_true", help="只预览 PDF 清单，不读取内容、不调用 API")
    ask = commands.add_parser("ask", help="第五步：检索知识库并生成附文章和 PDF 页码的回答")
    ask.add_argument("--question", help="未指定时使用 user_settings.py 中的 QA_SETTINGS.question")
    ask.add_argument("--database", type=Path)
    ask.add_argument("--output", type=Path, help="可选的 Markdown 输出路径")
    ask.add_argument("--retrieve-only", action="store_true", help="只调用向量模型检索，不调用回答模型")
    return parser


def main(argv=None):
    """程序总入口：解析命令行参数，执行对应任务，并将结果转成进程退出码。

    成功返回 0，任务失败返回 1，用户中断返回 130；argv 留空时读取终端传入的参数。
    """
    # Keep Chinese output readable when launched from Windows PowerShell.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "search":
            auto_literature_review = import_module("1abstract_read").auto_literature_review
            stats = auto_literature_review(args.query, args.question, args.year_start, args.year_end,
                                           args.max_papers, args.output, model=args.model)
            if args.download:
                import user_settings
                download_papers = import_module("2download_papers").download_papers
                from dataclasses import replace
                config = user_settings.RESEARCH_SETTINGS
                report = Path(stats["report"]) if stats.get("report") else (
                    replace(config, output_file=args.output).report_path if args.output else config.report_path)
                downloads = download_papers(report.with_suffix(".papers.json"))
                stats["failed"] = stats.get("failed", 0) + downloads["failed"]
        elif args.command == "download":
            download_papers = import_module("2download_papers").download_papers
            stats = download_papers(args.input, args.output, limit=args.limit, dry_run=args.dry_run)
        elif args.command == "index":
            stats = import_module("4knowledge_base").build_knowledge_base(
                args.input, args.database, limit=args.limit, dry_run=args.dry_run)
        elif args.command == "ask":
            stats = import_module("5ask_papers").ask_papers(
                args.question, args.database, args.output, retrieve_only=args.retrieve_only)
        elif args.command == "zotero-check":
            import user_settings
            ZoteroBridge = import_module("2download_papers").ZoteroBridge
            bridge = ZoteroBridge(user_settings.DOWNLOAD_SETTINGS)
            try:
                print(f"Zotero 插件连接正常：{bridge.ping()}")
            finally:
                bridge.close()
            return 0
        return 1 if stats.get("failed") else 0
    except KeyboardInterrupt:
        print("\n已停止；已落盘的结果会在下次运行时复用。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
