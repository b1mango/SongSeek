import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from pydantic import BaseModel

from .downloader import verify
from .errors import SongSeekError, public_error
from .service import Service


def json_dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=lambda v: v.model_dump() if isinstance(v, BaseModel) else str(v))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="songseek", description="觅音：本地歌曲检索、审核与下载")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("app")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true")
    imp = commands.add_parser("import")
    imp.add_argument("source")
    imp.add_argument("--name")
    commands.add_parser("playlists")
    search = commands.add_parser("search")
    search.add_argument("playlist_id", type=int)
    search.add_argument("--json", action="store_true")
    one = commands.add_parser("search-one")
    one.add_argument("query")
    one.add_argument("--duration", type=float)
    one.add_argument("--json", action="store_true")
    review = commands.add_parser("review")
    review.add_argument("playlist_id", type=int)
    download = commands.add_parser("download")
    download.add_argument("playlist_id", type=int)
    check = commands.add_parser("verify")
    check.add_argument("paths", nargs="+")
    providers = commands.add_parser("providers")
    providers.add_argument("--health", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("playlist_id", type=int)
    export.add_argument("-o", "--output")
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "app":
            from .desktop import main as desktop_main
            sys.argv = [sys.argv[0]]
            desktop_main()
            return
        if args.command == "serve":
            from .server import create_app
            if not 1 <= args.port <= 65535:
                raise SongSeekError("端口必须在 1 到 65535 之间", "invalid_port")
            if not args.no_open:
                timer = threading.Timer(.8, webbrowser.open, args=(f"http://127.0.0.1:{args.port}",))
                timer.daemon = True
                timer.start()
            uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
            return
        if args.command == "verify":
            failed = False
            for value in args.paths:
                path = Path(value).expanduser().resolve()
                try:
                    print(json_dump({"path": str(path), "ok": True, **verify(path)}))
                except Exception as error:
                    failed = True
                    print(json_dump({"path": str(path), "ok": False, "error": public_error(error)}))
            if failed:
                raise SystemExit(1)
            return
        service = Service()
        try:
            if args.command == "import":
                if args.source == "-":
                    parsed = service.parse(text=sys.stdin.read())
                elif args.source.startswith(("http://", "https://")):
                    parsed = service.parse(url=args.source)
                else:
                    path = Path(args.source)
                    parsed = service.parse(data=path.read_bytes(), filename=path.name)
                playlist = service.create_playlist(args.name or parsed.name, parsed.source, parsed.rows)
                print(json_dump(playlist))
                for warning in parsed.warnings:
                    print(warning, file=sys.stderr)
            elif args.command == "playlists":
                print(json_dump(service.playlists()))
            elif args.command in ("search", "download"):
                job = service.jobs.start(args.command, args.playlist_id)
                try:
                    job = service.jobs.wait(job.id)
                except KeyboardInterrupt:
                    service.jobs.cancel(job.id)
                    job = service.jobs.wait(job.id)
                if getattr(args, "json", False):
                    print(json_dump({"job": job, "tracks": service.tracks(args.playlist_id)}))
                else:
                    print(json_dump(job))
                    for track in service.tracks(args.playlist_id):
                        print(f"{track.id}\t{track.status}\t{track.title} - {track.artist}" + (f"\t{track.error}" if track.error else ""))
            elif args.command == "search-one":
                if not args.query.strip() or (args.duration is not None and args.duration <= 0):
                    raise SongSeekError("搜索词或时长不正确", "invalid_query")
                result = service.search_one(args.query, args.duration)
                if args.json:
                    print(json_dump(result))
                else:
                    print(f"{result['track'].id}\t{result['track'].status}")
                    for c in result["candidates"]:
                        print(f"{c.rank}. {c.verdict} {c.score:.1f}\t{c.title} - {c.artist}\t{c.provider_label} {c.quality.label}\t{c.duration}s")
            elif args.command == "review":
                for track in service.tracks(args.playlist_id):
                    if track.status != "needs_review":
                        continue
                    print(f"{track.id}. {track.title} - {track.artist}")
                    for c in service.candidates(track.id)[:3]:
                        print(f"  候选 {c.id}: {c.title} - {c.artist} | {c.provider_label} {c.quality.label} {c.duration}s | {c.verdict} {c.score:.1f}")
                        print("  " + "；".join(check.label for check in c.checks))
            elif args.command == "providers":
                print(json_dump(service.health() if args.health else sorted(service.config.providers, key=lambda p: p.order)))
            elif args.command == "export":
                content = service.export_csv(args.playlist_id)
                if args.output:
                    Path(args.output).write_text(content, encoding="utf-8", newline="")
                    print(str(Path(args.output).resolve()))
                else:
                    sys.stdout.write(content)
        finally:
            service.close()
    except Exception as error:
        print(f"错误：{public_error(error)}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
