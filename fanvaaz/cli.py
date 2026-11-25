"""Command line.

fanvaaz run                     collect live YouTube data and score it (the LangGraph agent)
fanvaaz run --source replay --snapshot FILE   re-score a recorded real collection
fanvaaz resume RUN_ID           continue a failed run from its last checkpoint
fanvaaz serve                   API + dashboard (+ MCP at /mcp)
fanvaaz mcp                     MCP server over stdio (Claude Desktop / Claude Code)
fanvaaz report                  latest run as JSON
fanvaaz profile list|show|use   what is tracked
fanvaaz tokens "text"           local token estimate vs Gemini's countTokens
fanvaaz graphs                  print both agent graphs as Mermaid
"""

from __future__ import annotations

import argparse
import json

from . import obs
from . import profile as profile_mod
from .config import get_settings
from .store import Store


def main(argv: list[str] | None = None) -> None:
    s = get_settings()
    obs.configure_logging(json_logs=s.log_json)
    p = argparse.ArgumentParser(prog="fanvaaz")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="collect live data, score and store a run")
    r.add_argument("--source", choices=["youtube", "replay"], default="youtube")
    r.add_argument("--snapshot", default=None, help="snapshot JSON for --source replay")
    r.add_argument("--keywords", nargs="*", default=None, help="defaults to the profile's keywords")
    r.add_argument("--no-llm", action="store_true", help="rules only, even if an LLM is configured")

    rs = sub.add_parser("resume", help="continue a failed run from its last completed node")
    rs.add_argument("run_id")

    sv = sub.add_parser("serve", help="start the API and dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    sub.add_parser("mcp", help="run the MCP server over stdio")
    sub.add_parser("report", help="print the latest run as JSON (for the two-pager)")
    sub.add_parser("graphs", help="print the LangGraph agents as Mermaid")
    tk = sub.add_parser(
        "tokens", help="estimate tokens locally and, with a Gemini key, compare to countTokens"
    )
    tk.add_argument("text")

    pr = sub.add_parser("profile", help="see or change what is tracked (brand, rivals, keywords)")
    pr_sub = pr.add_subparsers(dest="action", required=True)
    pr_sub.add_parser("list", help="example templates")
    pr_sub.add_parser("show", help="print the profile in use as JSON (a starting point for your own)")
    use = pr_sub.add_parser("use", help="switch to a template id or a profile JSON file")
    use.add_argument("ref")

    args = p.parse_args(argv)
    if args.cmd == "run":
        from .pipeline import run

        try:
            info = run(
                source=args.source,
                snapshot=args.snapshot,
                keywords=args.keywords,
                use_llm=not args.no_llm,
                actor="cli",
            )
        except (ValueError, RuntimeError) as e:
            raise SystemExit(f"fanvaaz run: {e}") from e
        print(info.model_dump_json(indent=2))
    elif args.cmd == "resume":
        from .pipeline import resume

        print(resume(args.run_id, actor="cli").model_dump_json(indent=2))
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("fanvaaz.api:app", host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
    elif args.cmd == "graphs":
        from .graph import ask_graph, collect_graph

        print("%% collection agent\n" + collect_graph.mermaid() + "\n%% ask agent\n" + ask_graph.mermaid())
    elif args.cmd == "tokens":
        from . import tokens

        out = {"estimate": tokens.estimate(args.text)}
        if s.llm_api_key and "generativelanguage" in (s.llm_base_url or ""):
            out["gemini_count_tokens"] = tokens.count_tokens_gemini(s.llm_api_key, s.llm_model, args.text)
        print(json.dumps(out))
    elif args.cmd == "report":
        st = Store.from_settings(s)
        latest = st.latest_run()
        if not latest:
            raise SystemExit("no runs yet: fanvaaz run (needs FANVAAZ_YOUTUBE_API_KEY)")
        out = {
            "run": latest.model_dump(),
            "reports": {k: st.report(latest.run_id, k).model_dump() for k in st.keywords(latest.run_id)},
            "insights": st.insights(latest.run_id),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "profile":
        st = Store.from_settings(s)
        if args.action == "list":
            try:
                current = profile_mod.sync_from(st).id
            except profile_mod.NoProfileError:
                current = None
            for p_ in profile_mod.presets().values():
                mark = "*" if p_.id == current else " "
                print(f"{mark} {p_.id:<20} {p_.name}: {p_.focus_brand} vs {', '.join(p_.rivals)}")
        elif args.action == "show":
            print(profile_mod.sync_from(st).model_dump_json(indent=2))
        else:
            try:
                new = profile_mod.load(args.ref)
            except ValueError as e:
                raise SystemExit(str(e)) from e
            st.save_profile(new.model_dump(), "cli")
            print(
                f"Now tracking {new.focus_brand} against {len(new.rivals)} rivals on {len(new.keywords)} keywords."
            )


if __name__ == "__main__":
    main()
