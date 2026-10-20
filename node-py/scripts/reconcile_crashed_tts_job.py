#!/usr/bin/env python3
"""Offline, exact-job recovery after a witnessed Linux native TTS process crash.

Dry-run is the default. This never starts a provider, submits inference, releases
an external reservation, or claims the lost CUDA synchronization callback ran.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "Owner regular file required")
        value = stream.read(limit + 1)
    require(len(value) <= limit, "Bounded file refused")
    return value


def command(args):
    return subprocess.check_output(args, text=True, timeout=20, stderr=subprocess.PIPE)


def observe(args, receipt):
    old = receipt["incarnation"]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    require(boot == old["bootId"], "Same boot required; cross-boot recovery not implemented")
    require(not Path(f"/proc/{old['pid']}").exists(), "Old PID still exists or was reused")
    props = dict(line.split("=", 1) for line in command([
        "systemctl", "--user", "show", args.unit,
        "--property=ActiveState,SubState,MainPID,Result,InvocationID,ExecMainCode,ExecMainStatus",
    ]).strip().splitlines())
    require(props.get("MainPID") == "0" and props.get("ActiveState") == "failed"
            and props.get("Result") == "core-dump" and props.get("ExecMainStatus") == "6"
            and props.get("InvocationID") == args.invocation_id, "Exact failed native incarnation required")
    journal = command(["journalctl", "--user", "-u", args.unit, "--no-pager", "-o", "json",
                       "--since", "@" + str(int(receipt["acceptedAt"])), "-n", "64",
                       "--grep", "CUDA error", "_PID=" + str(old["pid"]),
                       "_SYSTEMD_INVOCATION_ID=" + args.invocation_id])
    require(len(journal.encode()) <= 8 * 1024 * 1024, "Bounded journal required")
    entries = [json.loads(line) for line in journal.splitlines()]
    witnesses = [entry for entry in entries
                 if entry.get("_PID") == str(old["pid"])
                 and entry.get("_BOOT_ID") == boot.replace("-", "")
                 and entry.get("_SYSTEMD_INVOCATION_ID") == args.invocation_id
                 and "CUDA error: out of memory" in str(entry.get("MESSAGE", ""))]
    require(witnesses, "Same-incarnation kernel journal CUDA OOM witness absent")
    paths = {entry.get("_SYSTEMD_CGROUP") for entry in witnesses}
    require(len(paths) == 1, "Unique witnessed old cgroup required")
    group = paths.pop()
    expected = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{args.unit}"
    require(group == expected, "Exact owned user service cgroup required")
    require(not Path("/sys/fs/cgroup" + group).exists(), "Old cgroup still exists; populated-zero path not implemented")
    nvml = command(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_gpu_memory", "--format=csv,noheader"])
    pids = {line.split(",", 1)[0].strip() for line in nvml.splitlines() if line.strip()}
    require(str(old["pid"]) not in pids, "Old GPU process still exists")
    return {"observedAt": time.time(), "incarnation": old, "systemd": props,
            "kernelOomWitnesses": witnesses, "oldCgroup": group, "oldCgroupRemoved": True,
            "oldPidAbsent": True, "nvmlOldPidAbsent": True, "nvmlObservationSha256": sha(nvml.encode()),
            "normalCudaSynchronizeCallbackObserved": False}


def atomic_file(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--receipt-sha256", required=True, help="Exact existing disk receipt bytes")
    parser.add_argument("--unit", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", args.job_id), "Exact job ID required")
    require(re.fullmatch(r"[a-zA-Z0-9_.-]+\.service", args.unit), "Exact user unit name required")
    config_bytes = read_regular(Path(args.config), 1024 * 1024)
    require(sha(config_bytes) == args.config_sha256, "Config file bytes changed")
    config = json.loads(config_bytes)
    root = Path(config["stateRoot"])
    require(root.is_absolute() and not root.is_symlink(), "Actual absolute state root required")
    lock_fd = os.open(root / ".owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(lock_fd).st_mode), "Actual owner lock file required")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / args.job_id
        disk = read_regular(path / "receipt.json", 1024 * 1024)
        require(sha(disk) == args.receipt_sha256, "Disk receipt bytes changed")
        require(not path.is_symlink() and not (root / "jobs.sqlite3").is_symlink(), "Actual owner paths required")
        db = sqlite3.connect((root / "jobs.sqlite3").as_uri() + ("?mode=rw" if args.apply else "?mode=ro"),
                             uri=True, timeout=5)
        try:
            db.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
            row = db.execute("SELECT digest,body,state,receipt FROM jobs WHERE id=?", (args.job_id,)).fetchone()
            require(row is not None and row[0] == args.request_sha256 and row[2] == "running", "Exact running job CAS required")
            body, old = json.loads(row[1]), json.loads(row[3])
            require(sha(canonical(body)) == row[0], "Exact request/database identity required")
            require(json.loads(read_regular(path / "request.json", 1024 * 1024)) == body, "Actual retained request differs")
            disk_value = json.loads(disk)
            # This worker publishes receipt.json at acceptance and finalization;
            # SQLite alone records the intermediate running/admission fields.
            accepted_keys = {"jobId", "requestSha256", "incarnation", "unitKind", "source", "modelManifestSha256",
                             "configurationSha256", "acceptedAt", "fleetAuthorizationClaimed"}
            require(set(disk_value) == accepted_keys and disk_value == {key: old[key] for key in accepted_keys},
                    "Exact accepted disk receipt and current running identity required")
            require(old.get("source") == config["sourceIdentity"] and old.get("configurationSha256") == sha(canonical(config)), "Exact installed source/config required")
            require(old.get("jobId") == args.job_id and old.get("requestSha256") == args.request_sha256, "Receipt scope differs")
            require(old.get("localLeaseId") and "lease_id" in old.get("localGrant", {})
                    and old["localGrant"]["lease_id"] is None, "External reservation recovery not implemented")
            require({entry.name for entry in path.iterdir()} == {"request.json", "receipt.json"}, "Unexpected artifacts require separate recovery review")
            proof = observe(args, old)
            new = {**old, "outcome": "failed", "errorType": "NativeOwnerProcessAborted",
                   "reason": "Witnessed CUDA out-of-memory aborted the old native owner; no completed audio artifact exists",
                   "settlement": {"kind": "old-owner-death-kernel-cgroup-nvml-reconciliation", "incarnation": old["incarnation"],
                                  "physicalSettled": True, "executorBarrierPassed": False, "normalCompletion": False,
                                  "proof": proof},
                   "localLeaseReleased": False, "localLeaseTerminatedWithOwner": True,
                   "localLeaseScope": "old-inprocess-harmony-coordinator", "externalReservationReleased": False,
                   "nativeOutputRecovered": False}
            evidence = {"schema": "harmony.crashed-tts-reconciliation.v1", "jobId": args.job_id,
                        "requestSha256": args.request_sha256, "oldDiskReceiptSha256": args.receipt_sha256,
                        "oldDatabaseReceiptSha256": sha(row[3].encode()), "configSha256": args.config_sha256,
                        "proposedReceipt": new, "applyRequested": args.apply, "databaseUpdated": False,
                        "serviceStarted": False, "modelSubmitted": False}
            evidence_path = Path(args.evidence)
            require(not evidence_path.exists(), "Distinct evidence path required")
            if args.apply:
                # Retain recovery intent before CAS. A crash between DB commit and
                # receipt publication remains visible; this tool never blind-replays.
                with evidence_path.open("x") as stream:
                    json.dump(evidence, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                require(read_regular(Path(args.config), 1024 * 1024) == config_bytes, "Config changed during checks")
                observe(args, old)
                encoded = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
                changed = db.execute("UPDATE jobs SET state='failed',receipt=? WHERE id=? AND digest=? AND state='running' AND receipt=?",
                                     (encoded, args.job_id, args.request_sha256, row[3])).rowcount
                require(changed == 1, "Exact old job CAS lost")
                db.commit()
                atomic_file(path / "receipt.json", encoded.encode())
                evidence.update(databaseUpdated=True, diskReceiptSha256=sha(encoded.encode()))
                atomic_file(evidence_path, (json.dumps(evidence, indent=2) + "\n").encode())
            else:
                db.rollback()
                with evidence_path.open("x") as stream:
                    json.dump(evidence, stream, indent=2)
            print(json.dumps({"jobId": args.job_id, "applied": args.apply, "evidence": str(evidence_path),
                              "normalCudaCallback": False, "restart": False, "inference": False}))
        finally:
            db.close()
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
