"""
airlinesim command-line interface.

Usage:
    python -m airlinesim.cli list
    python -m airlinesim.cli run <scenario>
    python -m airlinesim.cli demo [--days N]
    python -m airlinesim.cli gui [--port N] [--no-browser] [--log-file PATH]
    python -m airlinesim.cli explore [--port N] [--no-browser]
    python -m airlinesim.cli probe [--offline] [--year Y --month M]
    python -m airlinesim.cli refresh [--check-only]

Scenarios: competitive, integration, crew, deadhead, roster, route, finance,
           cabin, weather, alliance, map, btsdata, routedata, databuilt,
           refresh_cx,
           explorer
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
    "cabin":       "airlinesim.scenarios.scenario_cabin",
    "weather":     "airlinesim.scenarios.scenario_weather",
    "alliance":    "airlinesim.scenarios.scenario_alliance",
    "map":         "airlinesim.scenarios.scenario_map",
    "btsdata":     "airlinesim.scenarios.scenario_btsdata",
    "routedata":   "airlinesim.scenarios.scenario_routedata",
    "databuilt":   "airlinesim.scenarios.scenario_databuilt",
    "refresh_cx":  "airlinesim.scenarios.scenario_refresh",
    "ai_competition": "airlinesim.scenarios.ai_competition",
    "session":     "airlinesim.scenarios.scenario_session",
    "explorer":    "airlinesim.scenarios.scenario_explorer",
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
    if getattr(args, "data", False):
        from airlinesim.databuilder import run_from_data
        run_from_data(days=args.days, hub=args.hub)
        return
    from airlinesim import build_demo_world, run
    _, engine = build_demo_world()
    run(engine, days=args.days)


def _setup_logging(args):
    """Turn on file logging for the commands that run a live session."""
    from airlinesim import gamelog
    if getattr(args, "no_log", False):
        return
    gamelog.configure(path=getattr(args, "log_file", None),
                      level=getattr(args, "log_level", "INFO"),
                      max_bytes=int(getattr(args, "log_max_mb", 8) * 1024 * 1024),
                      backups=getattr(args, "log_backups", 5))
    print(gamelog.describe())


def cmd_gui(args, landing: str = "/", what: str = "GUI"):
    import webbrowser
    from airlinesim.server import run_server, lan_url

    _setup_logging(args)

    # One server serves both front ends — the game at / and the outcome
    # explorer at /explore.html. `explore` differs only in where it points the
    # browser, so both screens are always reachable from either command.
    httpd, hub = run_server(host="0.0.0.0", port=args.port,
                            world=getattr(args, "world", "demo"),
                            hub_iata=getattr(args, "hub", "ORD"),
                            cash=getattr(args, "cash", 0.0) or 0.0,
                            ai_cash=getattr(args, "ai_cash", None))
    page = landing.lstrip("/")
    local = f"http://127.0.0.1:{args.port}/{page}"
    print(f"AirlineSim {what} serving at:\n  {local}\n"
          f"  {lan_url(args.port)}/{page}  (other devices on this network)")
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
    if args_in and args_in[0] == "ingest":
        from airlinesim.btsdata.ingest import main as ingest_main
        sys.exit(ingest_main(args_in[1:]))
    if args_in and args_in[0] == "refresh":
        from airlinesim.btsdata.refresh import main as refresh_main
        sys.exit(refresh_main(args_in[1:]))

    parser = argparse.ArgumentParser(prog="airlinesim",
        description="Airline asset & resource management simulator.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list available scenarios").set_defaults(func=cmd_list)

    pr = sub.add_parser("run", help="run a named scenario")
    pr.add_argument("scenario", help="scenario name (see 'list')")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("demo", help="run the built-in two-carrier demo")
    pd.add_argument("--days", type=int, default=60, help="days to simulate")
    pd.add_argument("--data", action="store_true",
                    help="build the world from the BTS corpus instead of "
                         "hand-authored constants")
    pd.add_argument("--hub", default="ORD", help="hub airport for --data")
    pd.set_defaults(func=cmd_demo)

    sub.add_parser("probe", add_help=False,
                   help="verify BTS data sources; all flags pass through "
                        "(try: probe --help)")
    sub.add_parser("ingest", add_help=False,
                   help="load BTS exports into the SQLite warehouse "
                        "(try: ingest --help)")
    sub.add_parser("refresh", add_help=False,
                   help="refresh the corpus: staleness, fetch, distill, diff "
                        "(try: refresh --help)")

    pg = sub.add_parser("gui", help="launch the browser-based game GUI")
    pg.add_argument("--port", type=int, default=8765, help="port to serve on")
    pg.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    pg.add_argument("--world", choices=("demo", "data"), default="data",
                     help="the BTS-corpus network with network-planning AI "
                          "(default), or the two-airport demo sandbox")
    pg.add_argument("--hub", default="ORD", help="hub airport for --world data")
    # Starting cash is the difficulty dial: same rules, different runway.
    # Omitted, both auto-size off the down payments the starting fleet needs.
    pg.add_argument("--cash", type=float, default=0.0,
                    help="your starting cash in dollars (default: auto-size)")
    pg.add_argument("--ai-cash", type=float, default=None, dest="ai_cash",
                    help="each rival's starting cash (default: same as yours)")
    pg.set_defaults(func=cmd_gui)

    pe = sub.add_parser("explore",
                        help="launch the outcome explorer GUI (branch states, "
                             "run them, compare results)")
    pe.add_argument("--port", type=int, default=8765, help="port to serve on")
    pe.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    pe.add_argument("--world", choices=("demo", "data"), default="demo",
                     help="demo sandbox, or the BTS-corpus network with network-planning AI")
    pe.add_argument("--hub", default="ORD", help="hub airport for --world data")
    pe.set_defaults(func=lambda a: cmd_gui(a, "/explore.html", "Outcome Explorer"))

    # Logging flags on every command that runs a live session. Defaults are
    # sized for a 24-hour play session — see gamelog's module docstring.
    for p in (pg, pe):
        p.add_argument("--log-file", default=None,
                       help="where to write the debug log "
                            "(default: ~/.airlinesim/logs/airlinesim.log)")
        p.add_argument("--log-level", default="INFO",
                       choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                       help="log verbosity (default: INFO)")
        p.add_argument("--log-max-mb", type=float, default=4.0,
                       help="max size of one log file in MB (default: 4)")
        p.add_argument("--log-backups", type=int, default=5,
                       help="rotated files kept alongside the live one "
                            "(default: 5, so 6 files / 24 MB total)")
        p.add_argument("--no-log", action="store_true",
                       help="disable file logging entirely")

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
