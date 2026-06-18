"""V0 reversibility engine: capture a recovery point BEFORE a destructive action,
restore it in one command. File-based (works for SQLite .db, configs, any file)."""
import os, shutil, time, json

RDIR = ".demo_cli_recovery"

def _rdir(target):
    d = os.path.join(os.path.dirname(os.path.abspath(target)) or ".", RDIR)
    os.makedirs(d, exist_ok=True)
    return d

def can_capture(target_path):
    return bool(target_path) and os.path.exists(target_path)

def capture(target_path):
    if not can_capture(target_path):
        return None
    d = _rdir(target_path)
    ts = time.strftime("%Y%m%d-%H%M%S")
    rp = os.path.join(d, f"{os.path.basename(target_path)}.{ts}.bak")
    shutil.copy2(target_path, rp)
    ref = {"target": os.path.abspath(target_path), "recovery_point": rp, "captured_at": ts}
    with open(os.path.join(d, "index.jsonl"), "a") as f:
        f.write(json.dumps(ref) + "\n")
    return ref

def restore(ref):
    if not ref or not os.path.exists(ref["recovery_point"]):
        return False
    shutil.copy2(ref["recovery_point"], ref["target"])
    return True

def latest(base="."):
    idx = os.path.join(base, RDIR, "index.jsonl")
    if not os.path.exists(idx):
        return None
    last = None
    with open(idx) as f:
        for line in f:
            line = line.strip()
            if line:
                last = json.loads(line)
    return last
