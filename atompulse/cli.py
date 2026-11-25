"""Command line: `atompulse run`, `atompulse serve`, `atompulse report`."""

from __future__ import annotations

import argparse
import json
import logging

from .config import DEFAULT_KEYWORDS, get_settings
from .pipeline import run
from .store import Store


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="atompulse")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="collect, score and store a run")
    r.add_argument("--source", choices=["youtube", "demo"], default="demo")
    r.add_argument("--keywords", nargs="*", default=DEFAULT_KEYWORDS)
    r.add_argument("--no-llm", action="store_true", help="rules only, even if an LLM is configured")

    s = sub.add_parser("serve", help="start the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    sub.add_parser("report", help="print the latest run as JSON (for the two-pager)")

    args = p.parse_args(argv)
    if args.cmd == "run":
        info = run(source=args.source, keywords=args.keywords, use_llm=not args.no_llm)
        print(info.model_dump_json(indent=2))
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("atompulse.api:app", host=args.host, port=args.port)
    elif args.cmd == "report":
        st = Store(get_settings().db_path)
        latest = st.latest_run()
        if not latest:
            raise SystemExit("no runs yet: atompulse run --source demo")
        out = {
            "run": latest.model_dump(),
            "reports": {k: st.report(latest.run_id, k).model_dump() for k in st.keywords(latest.run_id)},
            "insights": st.insights(latest.run_id),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
