"""
scenario_explorer — outcome-space exploration check.

The explorer's whole premise is that engine.py is deterministic: a forked state
re-run with the same edits gives the same answer, so a branching tree is a map
of outcomes rather than a pile of noise. That is a property of the *engine*,
not of explorer.py, and nothing else in the suite asserts it — so if someone
adds a `random` call to a subsystem, this is the scenario that goes red.

Also checks that the mutation knobs actually move the simulation. A knob wired
to a field no subsystem reads is worse than a missing one: it silently reports
"no effect" for every value, which reads like a finding. `fuel_index` on
MarketConditions was exactly that (declared, never read), which is why the fuel
knob drives FuelMarket.base_price_per_l instead.
"""

from airlinesim.explorer import (
    MUTATION_KINDS, Mutation, ScenarioTree, evaluate_derivation, linspace,
    validate_derivation,
)


def _human(node):
    return next(p for p in node.metrics["players"] if p["is_human"])


def main():
    print("=== OUTCOME EXPLORER ===\n")
    tree = ScenarioTree()
    root = tree.nodes[tree.root_id]
    print(f"root: day {root.metrics['day']}, "
          f"{len(root.metrics['players'])} carriers, "
          f"{len(root.blob) / 1024:.0f} KB of state")

    checks = []

    # -- 1. determinism: the same branch twice must agree exactly -------
    a = tree.branch(tree.root_id, (Mutation("price_scale", "*", 1.2),), cycles=45)
    b = tree.branch(tree.root_id, (Mutation("price_scale", "*", 1.2),), cycles=45)
    same = _human(a)["net_worth"] == _human(b)["net_worth"] and _human(a)["pax"] == _human(b)["pax"]
    print(f"\ndeterminism: two identical branches -> "
          f"${_human(a)['net_worth']:,.6f} vs ${_human(b)['net_worth']:,.6f}")
    checks.append(("identical branches produce identical outcomes", same))

    # -- 2. isolation: a child must not mutate its parent ---------------
    root_after = tree.nodes[tree.root_id]
    checks.append(("branching leaves the parent state untouched",
                   root_after.metrics["day"] == 0
                   and _human(root_after)["net_worth"] == _human(root)["net_worth"]))

    # -- 3. every declared knob must actually move something ------------
    print("\nknob sensitivity (45 cycles, low vs high):")
    probes = {
        "price_scale":  ("*", 0.7, 1.5),
        "price":        ("*", 120.0, 320.0),
        "frequency":    ("*", 1, 4),
        "cash":         ("*", 1e6, 9e6),
        "fuel_price":   ("*", 0.5, 3.0),
        "demand_scale": ("*", 0.4, 1.0),
    }
    assert set(probes) == set(MUTATION_KINDS), "probe list drifted from MUTATION_KINDS"
    for kind, (target, lo, hi) in probes.items():
        n_lo = tree.branch(tree.root_id, (Mutation(kind, target, lo),), cycles=45)
        n_hi = tree.branch(tree.root_id, (Mutation(kind, target, hi),), cycles=45)
        w_lo, w_hi = _human(n_lo)["net_worth"], _human(n_hi)["net_worth"]
        moved = w_lo != w_hi
        print(f"  {kind:13s} {lo:>8g} -> ${w_lo/1e6:7.3f}M | "
              f"{hi:>8g} -> ${w_hi/1e6:7.3f}M  {'move' if moved else 'NO EFFECT'}")
        checks.append((f"'{kind}' changes the outcome", moved))

    # -- 4. sweeps and the node cap -------------------------------------
    small = ScenarioTree(max_nodes=8)
    kids = small.sweep(small.root_id, "price_scale", "*", linspace(0.8, 1.4, 4), cycles=20)
    checks.append(("sweep creates one child per value", len(kids) == 4))
    capped = False
    try:
        small.sweep(small.root_id, "price_scale", "*", linspace(0.8, 1.4, 10), cycles=5)
    except ValueError:
        capped = True
    checks.append(("node cap refuses an oversized sweep", capped))

    # -- 5. expand is breadth**depth ------------------------------------
    exp = ScenarioTree(max_nodes=50)
    made = exp.expand(exp.root_id, "price_scale", "*", [0.9, 1.1], cycles=10, depth=3)
    checks.append(("expand builds breadth**depth nodes", len(made) == 2 + 4 + 8))
    checks.append(("expand reaches the requested depth",
                   max(n.depth for n in made) == 3))

    # -- 6. pruning removes the whole subtree ---------------------------
    before = len(exp.nodes)
    first_child = next(n for n in exp.nodes.values() if n.parent_id == exp.root_id)
    removed = exp.delete(first_child.node_id)
    checks.append(("delete prunes the entire subtree",
                   removed == 1 + 2 + 4 and len(exp.nodes) == before - removed))

    # -- 7. derivations: useful ones work, dangerous ones don't ---------
    metrics = a.metrics
    val, err = evaluate_derivation("human.net_worth > ai.net_worth", metrics)
    checks.append(("boolean derivation evaluates", err is None and isinstance(val, bool)))
    val, err = evaluate_derivation("round(human.load_factor, 2)", metrics)
    checks.append(("numeric derivation evaluates", err is None and isinstance(val, float)))

    unsafe = [
        "__import__('os').system('true')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "human.__class__",
        "[x for x in ()]",
        "(lambda: 1)()",
    ]
    blocked = [e for e in unsafe if validate_derivation(e) is not None]
    print(f"\nderivation sandbox: {len(blocked)}/{len(unsafe)} hostile expressions rejected")
    checks.append(("every unsafe derivation is rejected", len(blocked) == len(unsafe)))
    checks.append(("an unknown field is reported, not crashed",
                   evaluate_derivation("human.nope > 1", metrics)[1] is not None))

    # -- 8. the recipe is recoverable -----------------------------------
    path = tree.path(a.node_id)
    checks.append(("path returns root -> node",
                   len(path) == 2 and path[0].node_id == tree.root_id
                   and path[-1].node_id == a.node_id))

    print("\n=== EXPLORER CHECKS ===")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allpass = all(ok for _, ok in checks)
    print(f"\n{'ALL CHECKS PASS' if allpass else 'SOME CHECKS FAILED'}")


if __name__ == "__main__":
    main()
