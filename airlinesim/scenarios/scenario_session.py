"""
SESSION — the real-time clock guard and the debug log.
======================================================

Two things that live in GameSession rather than the engine, and that nothing
else checks:

1. **The clock cannot fast-forward through a suspend.** The loop converts real
   time into sim time, so a laptop that sleeps for three hours used to hand
   the engine ~5,400 sim-days in one locked burst. Nothing in the engine was
   wrong about what followed — every 84-month lease expired on schedule and
   BankingSubsystem correctly returned the aircraft, closing the routes those
   tails flew — but the player was asleep and could not re-lease. They came
   back to an empty airline. The AI looked immune only because it re-acquires
   on its own review cycle.

   So the loop now DISCARDS a suspend-sized gap and pauses. The checks below
   pin that: the sim must not advance, the human's fleet and routes must be
   exactly as they were, and the session must say why it paused.

2. **Log rotation is bounded.** A day-long session must not be able to fill a
   disk, so the handler's size cap has to actually hold.

Run: airlinesim run session
"""
import os
import tempfile
import time as _real_time

from airlinesim import gamelog
from airlinesim import game as game_mod
from airlinesim.game import new_game

# Three hours of wall clock — a lunch break with the lid shut. At the default
# 0.5 sim-days/second that is 5,400 sim-days, about fifteen years.
SLEEP_S = 3 * 3600.0


class _FakeClock:
    """Stands in for the `time` module inside game.py, so a three-hour gap
    costs the scenario nothing. Only the loop reads monotonic()."""
    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return _real_time.monotonic() + self.offset

    def sleep(self, seconds):
        _real_time.sleep(seconds)


def _settle(seconds=1.2):
    """Give the background loop a few wake-ups to react."""
    _real_time.sleep(seconds)


def main():
    print("=" * 70)
    print("SESSION — clock guard against suspend, and bounded logging")
    print("=" * 70)

    checks = []

    def check(label, ok, detail=""):
        checks.append((label, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")

    tmpdir = tempfile.mkdtemp(prefix="airlinesim-session-")
    log_path = os.path.join(tmpdir, "airlinesim.log")
    gamelog.configure(path=log_path, level="INFO")

    clock = _FakeClock()
    real_time_mod = game_mod.time
    game_mod.time = clock
    session = None
    try:
        session = new_game(world="data", hub="ORD", n_destinations=5)

        # --- stand up a one-route airline the way a player would ---
        cat = session.catalog()
        spec = next(a for a in cat["aircraft"] if a["max_seats"] >= 180)
        session.acquire_aircraft(spec["spec_id"], "N-SIM", "LEASE", base_iata="ORD")
        session.set_hub("ORD", True)
        session.hire_crew("COCKPIT", "ORD", 6, 120.0, (spec["type_rating"],))
        session.hire_crew("CABIN", "ORD", 12, 60.0, ())
        ok_route, msg = session.open_route("ORD-DEN", "N-SIM", 220.0, 2)
        check("a player can stand up a route from a cash-only start", ok_route, msg)

        human = session._human()
        before = (len(human.fleet), len(human.route_ops))

        session.resume()
        _settle(4.0)
        day_before = int(session.world.sim_time // 24)
        check("the clock runs while the session is resumed", day_before >= 1,
              f"day {day_before} after 4s at {session.speed} days/s")

        # --- the suspend ---
        clock.offset += SLEEP_S
        _settle(1.5)
        day_after = int(session.world.sim_time // 24)

        check("a suspend-sized clock gap is not replayed as sim time",
              day_after - day_before <= 1,
              f"day {day_before} -> {day_after} across a "
              f"{SLEEP_S / 3600:.0f}h gap ({SLEEP_S * session.speed:,.0f} "
              f"sim-days discarded)")
        check("the session pauses itself rather than running unattended",
              session.paused)
        check("the pause explains itself", bool(session.clock_notice),
              session.clock_notice)
        check("the human's fleet and routes survive the suspend",
              (len(human.fleet), len(human.route_ops)) == before,
              f"fleet {before[0]}->{len(human.fleet)}, "
              f"routes {before[1]}->{len(human.route_ops)}")
        check("the snapshot carries the notice to the GUI",
              bool(session.snapshot().get("clock_notice")))

        # --- resume clears it and the game carries on ---
        session.resume()
        check("resuming dismisses the notice", session.clock_notice == "")
        _settle(3.0)
        check("the clock runs again after resuming",
              int(session.world.sim_time // 24) > day_after,
              f"day {day_after} -> {int(session.world.sim_time // 24)}")
    finally:
        if session is not None:
            session.stop()
        game_mod.time = real_time_mod

    # --- the commands and the clock event are in the file ---
    with open(log_path, encoding="utf-8") as f:
        text = f.read()
    check("commands are recorded with their outcome",
          "cmd open_route" in text and "cmd acquire_aircraft" in text)
    check("the clock gap is recorded", "clock gap" in text,
          next((ln for ln in text.splitlines() if "clock gap" in ln), ""))

    # --- rotation actually bounds the bytes on disk ---
    rot_dir = tempfile.mkdtemp(prefix="airlinesim-rot-")
    rot_path = os.path.join(rot_dir, "rot.log")
    gamelog.configure(path=rot_path, level="INFO", max_bytes=4096, backups=2)
    log = gamelog.get("test")
    for i in range(4000):
        log.info("filler line %d — %s", i, "x" * 80)
    files = [os.path.join(rot_dir, f) for f in os.listdir(rot_dir)]
    total = sum(os.path.getsize(f) for f in files)
    check("rotation keeps at most backups+1 files", len(files) <= 3,
          f"{len(files)} files: {sorted(os.path.basename(f) for f in files)}")
    check("rotation bounds total bytes on disk", total <= 4096 * 3 * 1.1,
          f"{total} bytes written out of ~{4000 * 100} logged "
          f"(cap 4096 x 3)")

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(checks)} checks passed — "
          f"{'ALL CHECKS PASS' if passed == len(checks) else 'SOME CHECKS FAILED'}")


if __name__ == "__main__":
    main()
