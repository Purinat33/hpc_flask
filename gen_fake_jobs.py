#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Generate sacct-like, --parsable2 (pipe-delimited) CSV, including parents + steps.

Header/column order (EXACT):
User|JobID|JobName|Elapsed|TotalCPU|CPUTime|CPUTimeRAW|ReqTRES|AllocTRES|AveRSS|MaxRSS|TRESUsageInTot|TRESUsageOutTot|End|State|ExitCode|DerivedExitCode|ConsumedEnergyRaw|ConsumedEnergy|NodeList|AllocNodes

Conventions matched to your sample:
- Parent rows: User filled; JobName ~ 'interactive' (randomized); ReqTRES & AllocTRES filled.
- Step rows (".batch", ".0", ".1", ...): User is empty; ReqTRES usually empty; AllocTRES present.
- End timestamps: naive ISO (no Z).
- Delimiter: '|'.

Usage:
  python gen_fake_jobs.py --outfile test.csv --parents 3000 --users 18 \
      --start 2024-01-01 --end 2025-06-30 --seed 42
"""
from __future__ import annotations
import math
import random
import csv
import argparse
from datetime import datetime, timedelta


# ---------------------------- helpers ----------------------------


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s)


def iso_naive(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def fmt_d_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}-{h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def weighted_choice(d: dict):
    total = sum(d.values())
    x = random.uniform(0, total)
    acc = 0.0
    for k, w in d.items():
        acc += w
        if x <= acc:
            return k
    return next(iter(d))


def human_size_kmg(val_k: int) -> str:
    """Return K/M/G suffix like sacct AveRSS/MaxRSS. Input in KiB."""
    if val_k >= 1024**2:  # GiB in KiB
        return f"{val_k/(1024**2):.0f}G"
    if val_k >= 1024:     # MiB in KiB
        return f"{val_k/1024:.0f}M"
    return f"{val_k:.0f}K"


def mk_users(n: int):
    seeds = [
        "admin", "akara.sup", "surapol.gits", "maidev99", "gov1", "dave", "erin", "frank", "grace",
        "heidi", "ivan", "judy", "mallory", "nia", "oscar", "peggy", "trent", "victor", "wendy", "yuki"
    ]
    random.shuffle(seeds)
    return seeds[:max(1, min(n, 20))]


NODE_POOL = ["tau", "omega", "sigma", "alpha",
             "n[001-003]", "gpu03", "bigmem01"]
PARENT_JOBNAMES = ["interactive", "submit", "train", "inference", "etl"]
STEP_NAMES = ["bash", "python", "R", "srun", "sh"]

STATE_WEIGHTS = {"COMPLETED": 75, "FAILED": 10, "TIMEOUT": 6,
                 "PREEMPTED": 5, "CANCELLED by 749000022": 4}
STEP_COUNT_WEIGHTS = {0: 20, 1: 35, 2: 30, 3: 12, 4: 3}


def next_jobid(start=320000):
    i = start + random.randint(0, 20000)
    while True:
        i += 1
        yield str(i)

# ---------------------------- generator ----------------------------


COLUMNS = [
    "User", "JobID", "JobName", "Elapsed", "TotalCPU", "CPUTime", "CPUTimeRAW",
    "ReqTRES", "AllocTRES", "AveRSS", "MaxRSS", "TRESUsageInTot", "TRESUsageOutTot",
    "End", "State", "ExitCode", "DerivedExitCode", "ConsumedEnergyRaw", "ConsumedEnergy",
    "NodeList", "AllocNodes"
]


def make_parent_row(user: str, jobid: str, end_dt: datetime):
    # resources
    cpus = random.choice([1, 2, 4, 8, 16, 32])
    mem_gb = random.choice([8, 16, 32, 64, 128, 256, 512])
    gpus = random.choice([0, 0, 0, 1, 2])  # skew toward CPU jobs

    # wall-clock & cpu time
    elapsed_s = random.randint(10*60, 72*3600)  # 10 min .. 72h
    elapsed = fmt_d_hms(elapsed_s)

    # TotalCPU / CPUTime / CPUTimeRAW: sacct varies; we'll provide all 3
    util = random.uniform(0.4, 1.05)
    cpu_seconds = max(0, int(cpus * elapsed_s * util))
    totalcpu = fmt_d_hms(cpu_seconds)
    cputime = fmt_d_hms(max(cpu_seconds, elapsed_s))  # often >= elapsed
    cputimeraw = str(cpu_seconds)

    req_cpus = max(1, int(round(cpus * random.uniform(0.85, 1.15))))
    req_mem = max(1, int(round(mem_gb * random.uniform(0.85, 1.15))))
    req_tres = f"billing={max(1, cpus//2)},cpu={req_cpus}" + (f",gres/gpu={gpus}" if gpus else "") + \
        (f",mem={req_mem}G" if random.random() < 0.5 else "") + ",node=1"
    alloc_tres = f"billing={max(1, cpus//2)},cpu={cpus}" + \
        (f",gres/gpu={gpus}" if gpus else "") + (",node=1")

    node = random.choice(NODE_POOL)
    state = weighted_choice(STATE_WEIGHTS)

    # exit codes (simple mapping)
    if state == "COMPLETED":
        exit_code = "0:0"
        d_exit = "0:0"
    elif state.startswith("CANCELLED"):
        exit_code = "0:9"
        d_exit = "0:0"
    elif state == "TIMEOUT":
        exit_code = "0:0"
        d_exit = "0:0"
    elif state == "PREEMPTED":
        exit_code = "0:9"
        d_exit = "0:9"
    else:  # FAILED
        exit_code = "0:15"
        d_exit = "0:0"

    # energy (as sacct columns)
    energy_raw = str(int((cpus * elapsed_s / 3600.0) * random.uniform(0, 40)))
    energy = str(int(float(energy_raw) * random.uniform(0.7, 1.2))
                 ) if random.random() < 0.3 else ""

    # Parent has no AveRSS/MaxRSS typically
    return {
        "User": user,
        "JobID": jobid,
        "JobName": random.choice(PARENT_JOBNAMES),
        "Elapsed": elapsed,
        # sometimes blank/zero like your sample
        "TotalCPU": totalcpu if random.random() < 0.6 else "00:00:00",
        "CPUTime": cputime,
        "CPUTimeRAW": cputimeraw,
        "ReqTRES": req_tres,
        "AllocTRES": alloc_tres,
        "AveRSS": "",
        "MaxRSS": "",
        "TRESUsageInTot": "",
        "TRESUsageOutTot": "",
        "End": iso_naive(end_dt),
        "State": state,
        "ExitCode": exit_code,
        "DerivedExitCode": d_exit,
        "ConsumedEnergyRaw": energy_raw if random.random() < 0.5 else "",
        "ConsumedEnergy": energy,
        "NodeList": node,
        "AllocNodes": "1",
    }


def make_step_rows(parent: dict, n_steps: int):
    if n_steps <= 0:
        return []
    rows = []
    elapsed_parent_s = parse_elapsed_seconds(parent["Elapsed"])
    remaining = elapsed_parent_s
    cpus = parse_cpus_from_tres(parent["AllocTRES"])
    gpus = parse_gpus_from_tres(parent["AllocTRES"])

    for i, sid in enumerate([".batch"] + [f".{k}" for k in range(n_steps-1)]):
        # split elapsed roughly across steps; last takes remainder
        if i == n_steps - 1:
            step_s = max(1, remaining)
        else:
            step_s = max(1, int(remaining * random.uniform(0.2, 0.6)))
        remaining = max(0, remaining - step_s)

        util = random.uniform(0.35, 1.0)
        cpu_used_s = int(cpus * step_s * util)

        # rss in KiB, then convert
        # pick average fraction of some 'mem'; if we don't know mem, just randomize
        mem_k = random.randint(20_000, 800_000_000)  # 20MB .. 800GB in KiB
        averss = human_size_kmg(mem_k)
        maxrss = averss  # keep simple

        # TRESUsage* — throw in a few keys; format doesn't matter for your pipeline
        tres_in = f"cpu={fmt_d_hms(int(cpu_used_s * random.uniform(0.95, 1.05)))},energy=0,mem={averss},vmem={averss}"
        tres_out = f"energy=0,fs/disk={random.randint(10**9, 10**13)}"

        # step AllocTRES; keep cpu and gpu, mem=0 in many sacct views
        alloc_tres = f"cpu={max(1, cpus//max(1, n_steps))}" + \
            (f",gres/gpu={gpus}" if gpus else "") + ",mem=0,node=1"

        rows.append({
            "User": "",  # IMPORTANT: empty on steps
            "JobID": parent["JobID"] + sid,
            "JobName": random.choice(STEP_NAMES),
            "Elapsed": fmt_d_hms(step_s),
            "TotalCPU": fmt_d_hms(cpu_used_s),
            "CPUTime": fmt_d_hms(max(cpu_used_s, step_s)),
            "CPUTimeRAW": str(cpu_used_s),
            "ReqTRES": "" if random.random() < 0.9 else parent["ReqTRES"],
            "AllocTRES": alloc_tres,
            "AveRSS": averss,
            "MaxRSS": maxrss,
            "TRESUsageInTot": tres_in,
            "TRESUsageOutTot": tres_out,
            "End": parent["End"],
            "State": parent["State"] if not parent["State"].startswith("CANCELLED") else parent["State"],
            "ExitCode": parent["ExitCode"],
            "DerivedExitCode": parent["DerivedExitCode"],
            "ConsumedEnergyRaw": "0",
            "ConsumedEnergy": "0",
            "NodeList": parent["NodeList"],
            "AllocNodes": parent["AllocNodes"],
        })
    return rows


def parse_elapsed_seconds(s: str) -> int:
    # s can be HH:MM:SS or D-HH:MM:SS
    if "-" in s:
        d, rest = s.split("-", 1)
        d = int(d)
    else:
        d, rest = 0, s
    h, m, s2 = rest.split(":")
    return d*86400 + int(h)*3600 + int(m)*60 + int(s2)


def parse_cpus_from_tres(tres: str) -> int:
    # crude: look for "cpu=N"
    for token in tres.split(","):
        if token.startswith("cpu="):
            try:
                return int(token.split("=", 1)[1])
            except:  # noqa: E722
                pass
    return 1


def parse_gpus_from_tres(tres: str) -> int:
    for token in tres.split(","):
        if token.startswith("gres/gpu="):
            try:
                return int(token.split("=", 1)[1])
            except:  # noqa: E722
                pass
    return 0

# ---------------------------- main ----------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outfile", required=True)
    ap.add_argument("--parents", type=int, default=3000,
                    help="number of PARENT jobs (each adds 1..4 steps)")
    ap.add_argument("--users", type=int, default=12,
                    help="distinct users (<=20)")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=datetime.now().date().isoformat())
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--pct_parent_only", type=float, default=20.0,
                    help="percent of jobs with NO steps (0..100)")

    args = ap.parse_args()

    random.seed(args.seed)
    users = mk_users(args.users)
    jobids = next_jobid()
    start_dt = parse_date(args.start)
    end_dt = parse_date(args.end)
    span_s = max(1, int((end_dt - start_dt).total_seconds()))
    parent_only = (random.random() < (args.pct_parent_only / 100.0))
    n_steps = 0 if parent_only else weighted_choice(
        {1: 45, 2: 35, 3: 15, 4: 5})
    if n_steps > 0:
        rows.extend(make_step_rows(p, n_steps))

    rows = []
    for _ in range(args.parents):
        end_offset = random.randint(0, span_s)
        end_dt_job = start_dt + timedelta(seconds=end_offset)
        p = make_parent_row(random.choice(users), next(jobids), end_dt_job)
        rows.append(p)
        n_steps = weighted_choice(STEP_COUNT_WEIGHTS)
        if n_steps > 0:
            rows.extend(make_step_rows(p, n_steps))

    # write pipe-delimited with header
    with open(args.outfile, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS,
                           delimiter="|", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(
        f"wrote {len(rows)} rows ({args.parents} parents + steps) to {args.outfile}")


if __name__ == "__main__":
    main()
