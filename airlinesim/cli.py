"""
airlinesim command-line interface.

Usage:
    python -m airlinesim.cli list
    python -m airlinesim.cli run <scenario>
    python -m airlinesim.cli demo [--days N]
    python -m airlinesim.cli gui [--port N] [--no-browser]
    python -m airlinesim.cli probe [--offline] [--year Y --month M]

Scenarios: competitive, integration, crew, deadhead, roster, route, finance,
           btsdata
"""
import argparse
import importlib
import sys

SCENARIOS = {
    "competitive": "airlinesim.scenarios.competitive",
    "integration": "airlinesim.scenarios.integration",
    "crew":        "airlinesim.scenarios.scenario_crew",
    "deadhead":    "airlinesim.scenarios.scenario_deadhead",
    "roster":      "airlinesim.scenarios.scenario_roster",
    "route":       "airlinesim.scenarios.scenario_route",
    "finance":     "airlinesim.scenarios.scenario_finance_cabin",
    "btsdata":     "airlinesim.scenarios.scenario_btsdata",
}


def cmd_list(_):
    print("Available scenarios:\n")
    for name in SCENARIOS:
        print(f"  {name}")
    print("\nRun one with:  python -m airlinesim.cli run <scenario>")


def cmd_run(args):
    key = args.scenario
    if key not in SCENARIOS:
        print(f"Unknown scenario '{key}'. Options: {', '.join(SCENARIOS)}")
        sys.exit(1)
    mod = importlib.import_module(SCENARIOS[key])
    if hasattr(mod, "main"):
        mod.main()
    else:
        print(f"Scenario '{key}' has no main() to run.")


def cmd_demo(args):
    from airlinesim import build_demo_world, run
    _, engine = build_demo_world()
    run(engine, days=args.days)


def cmd_gui(args):
    import webbrowser
    from airlinesim.server import run_server, lan_url

    httpd, hub = run_server(host="0.0.0.0", port=args.port)
    local = f"http://127.0.0.1:{args.port}"
    print(f"AirlineSim GUI serving at:\n  {local}\n  {lan_url(args.port)}  (other devices on this network)")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(local)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.session.stop()
        httpd.shutdown()


def main(argv=None):
    # `probe` forwards every remaining flag to the ingest probe's own parser.
    # Intercepted before argparse because REMAINDER doesn't reliably keep
    # option-like tokens away from the top-level parser.
    args_in = sys.argv[1:] if argv is None else list(argv)
    if args_in and args_in[0] == "probe":
        from airlinesim.btsdata.probe import main as probe_main
        sys.exit(probe_main(args_in[1:]))

    parser = argparse.ArgumentParser(prog="airlinesim",
        description="Airline asset & resource management simulator.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list available scenarios").set_defaults(func=cmd_list)

    pr = sub.add_parser("run", help="run a named scenario")
    pr.add_argument("scenario", help="scenario name (see 'list')")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("demo", help="run the built-in two-carrier demo")
    pd.add_argument("--days", type=int, default=60, help="days to simulate")
    pd.set_defaults(func=cmd_demo)

    sub.add_parser("probe", add_help=False,
                   help="verify BTS data sources; all flags pass through "
                        "(try: probe --help)")

    pg = sub.add_parser("gui", help="launch the browser-based game GUI")
    pg.add_argument("--port", type=int, default=8765, help="port to serve on")
    pg.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    pg.set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
