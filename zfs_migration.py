#!/usr/bin/env python3
"""Deterministic, resumable ZFS dataset promotion and migration script."""

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    TimeElapsedColumn,
)

# ==========================================
# CONFIGURATION & STATE
# ==========================================

LOG_FILE = (
    Path("/tmp") / f"zfs_migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)

ACTIVE_PROCESSES: "list[subprocess.Popen]" = []
SHUTTING_DOWN = False
ZFS_LOCK = threading.Lock()
FAILED_JOBS: "list[str]" = []


@dataclass
class RcloneConfig:
    """Configuration for rclone transfer performance."""

    transfers: int = 32
    checkers: int = 16
    buffer_size: str = "512M"
    multi_thread_streams: int = 8
    multi_thread_cutoff: str = "4G"


# rclone progress output regex
# Matches: Transferred:    1.234 GiB / 10.000 GiB, 12.3%, 100.0 MiB/s, ETA 0m30s
rclone_progress_re = re.compile(
    r"Transferred:\s+([\d.,]+\s+[A-Za-z]+)\s*/\s*[\d.,]+\s*[A-Za-z]+,\s*"
    r"(\d+(?:\.\d+)?)%,\s*([\d.,]+\s+[A-Za-z]+/s)"
)

console = Console()

progress = Progress(
    SpinnerColumn(),
    TextColumn("[bold blue]{task.description}"),
    BarColumn(),
    TaskProgressColumn(),
    TextColumn("[cyan]{task.fields[transferred]}"),
    TextColumn("[magenta]{task.fields[speed]}"),
    TextColumn("[yellow]ETA:"),
    TimeRemainingColumn(),
    TextColumn("[bright_black]Elapsed:"),
    TimeElapsedColumn(),
    console=console,
    transient=False,
)

# ==========================================
# LOGGING SYSTEM
# ==========================================


def write_log(level: str, color: str, message: str) -> None:
    """Write a log entry to both file and console."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{level}] {message}\n")

    formatted_msg = (
        f"[[bright_black]{timestamp}[/bright_black]] "
        f"[[{color}]{level}[/{color}]] {message}"
    )
    if progress and progress.live and progress.live.is_started:
        progress.console.print(formatted_msg)
    else:
        console.print(formatted_msg)


def log_info(msg: str) -> None:
    write_log("INFO", "blue", msg)


def log_warn(msg: str) -> None:
    write_log("WARN", "yellow", msg)


def log_error(msg: str) -> None:
    write_log("ERROR", "red", msg)


def log_ok(msg: str) -> None:
    write_log("OK", "green", msg)


def log_step(msg: str) -> None:
    write_log("STEP", "cyan", msg)


# ==========================================
# CLEANUP & CTRL+C HANDLING
# ==========================================


def kill_all_children() -> None:
    """Terminate all tracked child processes."""
    for p in ACTIVE_PROCESSES:
        try:
            p.terminate()
        except Exception as e:
            log_warn(f"Failed to terminate process {p.pid}: {e}")
    subprocess.run(["pkill", "-P", str(os.getpid())], capture_output=True)
    subprocess.run(["pkill", "-f", "rclone"], capture_output=True)


def sigint_handler(signum: int, frame: "object | None") -> None:  # noqa: ANN401
    """Handle SIGINT/SIGTERM by shutting down workers."""
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    log_error("CTRL+C received — terminating workers immediately")
    kill_all_children()
    sys.exit(130)


signal.signal(signal.SIGINT, sigint_handler)
signal.signal(signal.SIGTERM, sigint_handler)
atexit.register(kill_all_children)

# ==========================================
# CONTEXT VALIDATION & HELPERS
# ==========================================


def get_zfs_context(target_path: Path) -> tuple[str, str]:
    """Extract pool and base dataset name from a mount path under /mnt."""
    mnt_path = Path("/mnt")
    try:
        rel_path = target_path.resolve().relative_to(mnt_path)
        parts = rel_path.parts
        if len(parts) < 2:
            raise ValueError
        pool = parts[0]
        base = "/".join(parts[1:])
        return pool, base
    except ValueError:
        log_error(
            f"Target path '{target_path.resolve()}' is not within /mnt/<pool>/<base>/"
        )
        sys.exit(1)


def is_valid_zfs_name(name: str) -> bool:
    """Check if a string is a valid ZFS dataset name."""
    if not re.match(r"^[A-Za-z0-9._ -]+$", name):
        return False
    if name != name.strip():
        return False
    return True


def should_skip_folder(name: str) -> bool:
    """Determine whether a folder name should be skipped during migration."""
    if name.startswith("."):
        return True
    skip_list = {
        "lost+found",
        "System Volume Information",
        "$RECYCLE.BIN",
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
        "Temporary Items",
    }
    return name in skip_list


def dataset_exists(dataset: str) -> bool:
    """Check if a ZFS dataset already exists."""
    res = subprocess.run(
        ["zfs", "list", "-H", "-o", "name", dataset],
        capture_output=True,
    )
    return res.returncode == 0


def create_dataset(dataset: str) -> None:
    """Create a ZFS dataset (thread-safe via ZFS_LOCK)."""
    with ZFS_LOCK:
        if not dataset_exists(dataset):
            res = subprocess.run(
                ["zfs", "create", dataset],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(
                    f"Failed to create dataset {dataset}: {res.stderr.strip()}"
                )


# ==========================================
# NFS SHARE CREATION (TrueNAS 26)
# ==========================================


def create_nfs_share(path: str, comment: str = "") -> bool:
    """Create an NFS share via midclt (local TrueNAS CLI).

    Runs `midclt call sharing.nfs.create` — no network auth needed since
    the script runs locally on TrueNAS. Returns True on success.
    """
    payload: dict[str, object] = {"path": path, "enabled": True}
    if comment:
        payload["comment"] = comment

    try:
        result = subprocess.run(
            ["midclt", "call", "sharing.nfs.create", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log_error(
                f"[NFS] Failed to create share for '{path}': "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return False
        return True
    except FileNotFoundError:
        log_error("[NFS] midclt not found — is this running on TrueNAS SCALE?")
        return False
    except subprocess.TimeoutExpired:
        log_error(f"[NFS] Timeout creating share for '{path}'")
        return False
    except Exception as e:
        log_error(f"[NFS] Unexpected error creating share for '{path}': {e}")
        return False


def nfs_share_exists(path: str) -> bool:
    """Check if an NFS share already exists for the given path via midclt.

    Queries all NFS shares and filters locally — the midclt CLI doesn't
    accept path-based filters for sharing.nfs.query.
    """
    try:
        result = subprocess.run(
            [
                "midclt",
                "call",
                "sharing.nfs.query",
                "[]",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        shares = json.loads(result.stdout)
        return any(share.get("path") == path for share in shares)
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        TypeError,
    ) as e:
        log_warn(f"[NFS] Failed to check share existence for {path}: {e}")
        return False


# ==========================================
# TRANSFER & VERIFICATION ENGINES
# ==========================================

# rsync progress regex (used for Phase 2 ACL sync)
_rsync_progress_re = re.compile(r"([\d.,]+[a-zA-Z]*)\s+(\d+)%\s+([\d.,]+[a-zA-Z]*/s)")


def run_transfer_with_progress(
    cmd: list[str],
    task_id: int,
    job_name: str,
    phase_desc: str,
    phase_color: str,
) -> tuple[bool, str]:
    """Run a transfer command (rclone or rsync) with progress parsing.

    Captures progress output and updates the Rich progress bar.
    On failure returns (False, error_message). On success returns (True, "").
    """
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ACTIVE_PROCESSES.append(p)
    buffer = bytearray()

    error_tail = deque(maxlen=20)
    file_count = 0

    progress.update(
        task_id,
        description=f"[cyan]{job_name} [{phase_color}]({phase_desc})",
        completed=0,
        transferred="0 B",
        speed="0 B/s",
    )

    while True:
        char = p.stdout.read(1)
        if not char:
            break

        if char in (b"\r", b"\n"):
            line = buffer.decode("utf-8", errors="replace").strip()
            if line:
                # Try rclone progress format first, then rsync
                rclone_match = rclone_progress_re.search(line)
                rsync_match = _rsync_progress_re.search(line)

                if rclone_match:
                    progress.update(
                        task_id,
                        description=(f"[cyan]{job_name} [{phase_color}]({phase_desc})"),
                        completed=float(rclone_match.group(2)),
                        transferred=rclone_match.group(1),
                        speed=rclone_match.group(3) or "0 B/s",
                    )
                elif rsync_match:
                    progress.update(
                        task_id,
                        description=(f"[cyan]{job_name} [{phase_color}]({phase_desc})"),
                        completed=float(rsync_match.group(2)),
                        transferred=rsync_match.group(1),
                        speed=rsync_match.group(3) or "0 B/s",
                    )
                else:
                    file_count += 1
                    if file_count % 1000 == 0:
                        progress.update(
                            task_id,
                            description=(
                                f"[cyan]{job_name} [{phase_color}]({phase_desc}...)"
                            ),
                        )

                    error_tail.append(line)
            buffer.clear()
        else:
            buffer.extend(char)

    p.wait()
    if p in ACTIVE_PROCESSES:
        ACTIVE_PROCESSES.remove(p)

    if p.returncode != 0:
        if SHUTTING_DOWN:
            return False, "Interrupted by user"
        err_msg = "\n".join(error_tail)
        if not err_msg.strip():
            err_msg = (
                f"Process killed/exited with code {p.returncode} "
                f"(No error output captured)"
            )
        return False, f"Transfer failed:\n{err_msg}"

    return True, ""


def run_rclone_move(
    src: str,
    dest: str,
    task_id: int,
    job_name: str,
    config: RcloneConfig | None = None,
) -> tuple[bool, str]:
    """Move data from src to dest with checksum verification, removing source files.

    Uses rclone move so each file is verified before being deleted
    from the temp directory. Source directories are cleaned up automatically
    (--delete-empty-src-dirs). This avoids the 2x disk space requirement of a
    full copy-then-verify approach.
    """
    if config is None:
        config = RcloneConfig()

    log_step(f"[Copy] Starting rclone transfer for: {job_name}")
    progress.update(task_id, description=f"[cyan]{job_name} [yellow](Checking...)")

    cmd = [
        "rclone",
        "move",
        "-P",
        f"{src}/",
        f"{dest}/",
        "--transfers=" + str(config.transfers),
        "--checkers=" + str(config.checkers),
        "--fast-list",
        "--buffer-size=" + config.buffer_size,
        "--use-mmap",
        "--multi-thread-streams=" + str(config.multi_thread_streams),
        "--multi-thread-cutoff=" + config.multi_thread_cutoff,
        "--no-traverse",
        "--delete-empty-src-dirs",
    ]
    return run_transfer_with_progress(
        cmd, task_id, job_name, "Copying+Verifying", "blue"
    )


# ==========================================
# JOB EXECUTION
# ==========================================


def process_job(
    job_name: str,
    is_resume: bool,
    global_task: int,
    pool: str,
    base: str,
    rclone_config: RcloneConfig | None = None,
) -> None:
    """Process a single migration job (copy, verify, create NFS share)."""
    if SHUTTING_DOWN:
        log_warn(f"Skipping {job_name}: shutting down")
        return

    temp_dir = f"{job_name}-tmp"
    target_dir = job_name
    dataset = f"{pool}/{base}/{job_name}"

    nfs_path = f"/mnt/{dataset}"

    if is_resume:
        log_warn(f"RESUME: {temp_dir} → {target_dir}")
    else:
        if dataset_exists(dataset):
            log_warn(f"Dataset exists → skipping data transfer: {dataset}")
            # Still ensure NFS share exists for existing datasets
            nfs_ok = True
            if not nfs_share_exists(nfs_path):
                log_step(f"[NFS] Creating share for: {nfs_path}")
                nfs_ok = create_nfs_share(
                    path=nfs_path,
                    comment=f"Migration share: {job_name}",
                )
            else:
                log_ok(f"[NFS] Share already exists: {nfs_path}")
            if nfs_ok:
                log_ok(f"[NFS] Share ready: {nfs_path}")
            else:
                log_warn(f"[NFS] Failed to create share for: {nfs_path}")
            progress.advance(global_task)
            return
        log_step(f"Processing: {job_name}")
        log_step(f"Renaming {target_dir} → {temp_dir}")
        os.rename(target_dir, temp_dir)

    if is_resume:
        log_step(f"Creating missing dataset (if applicable): {dataset}")
    create_dataset(dataset)
    log_ok(f"Dataset ready: {dataset}")
    if is_resume:
        os.makedirs(target_dir, exist_ok=True)

    task_id = progress.add_task(
        f"[cyan]{job_name}", total=100, transferred="0 B", speed="0 B/s"
    )

    # PHASE 1: [Copy] Incremental copy with checksum verification.
    # Source files are removed after successful verification, keeping disk
    # usage bounded throughout the transfer.
    success, err = run_rclone_move(
        temp_dir, target_dir, task_id, job_name, rclone_config
    )
    if not success:
        if not SHUTTING_DOWN:
            log_error(f"[Copy] Failed for {job_name}.\nReason: {err}")
            FAILED_JOBS.append(job_name)
        progress.update(
            task_id,
            description=f"[red]{job_name} [white](Failed)",
            visible=True,
        )
        return

    # rclone move already deleted all source files from temp_dir
    # (verified before each deletion). No cleanup needed.
    progress.update(
        task_id,
        description=f"[green]{job_name} [white](Done)",
        completed=100,
        transferred="",
        speed="",
        total=100,
    )
    log_ok(f"{'Resume ' if is_resume else ''}Complete: {job_name}")

    # PHASE 4: [NFS] Create NFS share via midclt (local TrueNAS CLI)
    nfs_ok = True  # Assume success — only False if creation fails
    if not nfs_share_exists(nfs_path):
        log_step(f"[NFS] Creating share for: {nfs_path}")
        nfs_ok = create_nfs_share(
            path=nfs_path,
            comment=f"Migration share: {job_name}",
        )
    else:
        log_ok(f"[NFS] Share already exists: {nfs_path}")
    if nfs_ok:
        log_ok(f"[NFS] Share created: {nfs_path}")
    else:
        log_warn(f"[NFS] Failed to create share for: {nfs_path}")

    progress.advance(global_task)


# ==========================================
# MAIN DISCOVERY & ROUTING
# ==========================================


def main() -> None:
    """Entry point: parse args, discover jobs, execute migration."""
    parser = argparse.ArgumentParser(
        description="ZFS Dataset Promotion Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Each migrated dataset is automatically exported as an NFS share\n"
            "via midclt (requires running on TrueNAS SCALE)."
        ),
    )
    parser.add_argument(
        "path", type=str, help="Target directory to migrate (e.g., /mnt/pool/base)"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Auto confirm")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Directory Worker Pool Size (Default: 4)",
    )
    # rclone performance options
    parser.add_argument(
        "--transfers",
        type=int,
        default=32,
        help="rclone concurrent file transfers per job (default: 32)",
    )
    parser.add_argument(
        "--checkers",
        type=int,
        default=16,
        help="rclone file checker threads per job (default: 16)",
    )
    parser.add_argument(
        "--buffer-size",
        type=str,
        default="512M",
        help="rclone in-memory buffer size per file (default: 512M)",
    )

    args = parser.parse_args()

    rclone_config = RcloneConfig(
        transfers=args.transfers,
        checkers=args.checkers,
        buffer_size=args.buffer_size,
    )

    target_path = Path(args.path)
    if not target_path.is_dir():
        log_error(f"Target path does not exist or is not a directory: {target_path}")
        sys.exit(1)

    pool, base = get_zfs_context(target_path)
    log_info(f"Target Directory: {target_path.resolve()}")
    log_info(f"ZFS Pool: {pool}")
    log_info(f"ZFS Base: {base}")
    log_info(f"Log file: {LOG_FILE}")
    os.chdir(target_path)

    dirs = [d for d in os.listdir(".") if os.path.isdir(d)]
    temporary_jobs: list[str] = []
    normal_jobs: list[str] = []

    for d in dirs:
        if should_skip_folder(d):
            log_info(f"Skipping (hidden/system): {d}")
            continue
        if d.endswith("-tmp-tmp"):
            log_warn(f"Prohibited nesting state detected, skipping: {d}")
            continue

        if d.endswith("-tmp"):
            base_name = d[:-4]
            if is_valid_zfs_name(base_name):
                temporary_jobs.append(base_name)
            else:
                log_warn(f"Invalid ZFS dataset name from tmp, skipping: {base_name}")
        else:
            if is_valid_zfs_name(d):
                normal_jobs.append(d)
            else:
                log_warn(f"Invalid ZFS dataset name, skipping: {d}")

    filtered_normal_jobs = [j for j in normal_jobs if j not in temporary_jobs]
    total_jobs = len(temporary_jobs) + len(filtered_normal_jobs)

    log_info(f"tmp jobs (priority): {len(temporary_jobs)}")
    log_info(f"normal jobs: {len(filtered_normal_jobs)}")
    log_info(f"total jobs: {total_jobs}")
    log_info(f"directory workers: {args.workers}")

    if total_jobs == 0:
        log_ok("Nothing to do.")
        return

    with progress:
        global_task = progress.add_task(
            "[green]Overall Progress",
            total=total_jobs,
            transferred="",
            speed="",
        )

        if not SHUTTING_DOWN:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures: dict[str, object] = {}  # value is concurrent.futures.Future

                for job in temporary_jobs:
                    f = executor.submit(
                        process_job,
                        job,
                        True,
                        global_task,
                        pool,
                        base,
                        rclone_config,
                    )
                    futures[f] = job  # type: ignore[misc]

                for job in filtered_normal_jobs:
                    f = executor.submit(
                        process_job,
                        job,
                        False,
                        global_task,
                        pool,
                        base,
                        rclone_config,
                    )
                    futures[f] = job  # type: ignore[misc]

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        log_error(f"Job exception: {e}")

    # Output strict final state verification
    if FAILED_JOBS:
        log_error(
            f"Completed with ERRORS. "
            f"{len(FAILED_JOBS)} dataset(s) failed: {', '.join(FAILED_JOBS)}"
        )
        sys.exit(1)
    else:
        if not SHUTTING_DOWN:
            log_ok("All complete successfully.")


if __name__ == "__main__":  # pragma: no cover
    main()
