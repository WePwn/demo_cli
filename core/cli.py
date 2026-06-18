import argparse, sys
from .core import Guard
from .report import shadow_report
from . import reversibility
from .demo import run as run_demo

def main(argv=None):
    p = argparse.ArgumentParser(prog="demo_cli", description="demo_cli V0 - proof before permission.")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="evaluate one command")
    c.add_argument("command"); c.add_argument("--target", default=None, help="path to snapshot if destructive")
    c.add_argument("--enforce", action="store_true")
    sub.add_parser("replay", help="run the recovery + structural-approval demo")
    rp = sub.add_parser("report", help="print the decision report"); rp.add_argument("--path", default="demo_cli_receipts.jsonl")
    sub.add_parser("undo", help="restore the most recent recovery point")
    a = p.parse_args(argv)

    if a.cmd == "check":
        g = Guard(mode="enforce" if a.enforce else "shadow")
        r, _ = g.evaluate(a.command, target_path=a.target)
        print(f"decision : {r.decision}\nenv      : {r.target_environment}\nrule     : {r.matched_rule}")
        print(f"approval : {r.structural_approval}\nreason   : {r.reason}")
        if r.recovery_point: print(f"recovery : {r.recovery_point}")
        if a.enforce and r.decision in ("BLOCK","ESCALATE"): print("\naction NOT permitted (enforce mode)."); sys.exit(3)
    elif a.cmd == "replay":
        run_demo()
    elif a.cmd == "report":
        print(shadow_report(a.path))
    elif a.cmd == "undo":
        ref = reversibility.latest(".")
        print("restored " + ref["target"] if ref and reversibility.restore(ref) else "no recovery point found")
    else:
        p.print_help()

if __name__ == "__main__":
    main()
