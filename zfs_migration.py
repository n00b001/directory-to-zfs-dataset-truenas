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
import time
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

# NFS share cache — populated once at startup to avoid redundant midclt calls
_NFS_SHARE_CACHE: "dict[str, bool] | None" = None


@dataclass
class RcloneConfig:
    """Configuration for rclone transfer performance."""

    transfers: int = 64
    checkers: int = 32
    buffer_size: str = "1024M"
    multi_thread_streams: int = 16
    multi_thread_cutoff: str = "4G"


def get_available_ram_gb() -> float:
    """Return available RAM in GB (total minus 4GB reserved for system services)."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                    return max(0, (total_kb / 1024 / 1024) - 4)
    except Exception:
        pass
    return 32.0  # fallback if we can't read it


def is_rotational_disk(path: str) -> bool:
    """Check if the ZFS pool's underlying disks are HDD (rotational).

    Scans lsblk output for devices mounted under /mnt with rotational=1.
    """
    res = subprocess.run(
        ["lsblk", "--dms", "-o", "NAME,ROTA,MOUNTPOINT"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    for line in res.stdout.splitlines():
        if "/mnt" in line:
            parts = line.strip().split()
            # ROTA is column 1 regardless of MOUNTPOINT position
            if len(parts) >= 2 and parts[1] == "1":
                return True
    return False


# rclone progress output regex
# Matches: Transferred:    1.234 GiB / 10.000 GiB, 12.3%, 100.0 MiB/s, ETA 0m30s
rclone_progress_re = re.compile(
    r"Transferred:\s+([\d.,]+\s+[A-Za-z]+)\s*/\s+([\d.,]+\s+[A-Za-z]+),\s*"
    r"(\d+(?:\.\d+)?)%,\s*([\d.,]+\s+[A-Za-z]+/s)"
)

console = Console()

# Dedicated console for log messages — must NOT share the Rich progress
# console, otherwise interleaved print() calls corrupt the ANSI rendering
# buffer and TimeRemainingColumn cannot compute/display ETA.
log_console = Console(force_terminal=True)

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
        # Flush the progress buffer first so Rich finishes its current
        # render cycle before we inject a raw log line.  This prevents
        # interleaved ANSI sequences from corrupting the ETA display.
        progress.refresh()
    log_console.print(formatted_msg)


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
    try:
        res = subprocess.run(
            ["zfs", "list", "-H", "-o", "name", dataset],
            capture_output=True,
            timeout=10,
        )
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


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


def _populate_nfs_cache() -> None:
    """Populate the NFS share cache by querying all shares once."""
    global _NFS_SHARE_CACHE
    try:
        result = subprocess.run(
            ["midclt", "call", "sharing.nfs.query", "[]"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            _NFS_SHARE_CACHE = {}
            return
        shares = json.loads(result.stdout)
        _NFS_SHARE_CACHE = {share.get("path", ""): True for share in shares}
    except Exception as e:
        log_warn(f"[NFS] Failed to populate cache: {e}")
        _NFS_SHARE_CACHE = {}


def nfs_share_exists(path: str) -> bool:
    """Check if an NFS share already exists for the given path via midclt.

    Uses a one-time cache populated at startup to avoid redundant
    midclt calls. Falls back to live query on cache miss or failure.
    """
    # Fast path: check in-memory cache
    if _NFS_SHARE_CACHE is not None:
        return _NFS_SHARE_CACHE.get(path, False)

    # Fallback: live query (should only happen if cache population failed)
    try:
        result = subprocess.run(
            ["midclt", "call", "sharing.nfs.query", "[]"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        shares = json.loads(result.stdout)
        found = any(share.get("path") == path for share in shares)
        # Update cache if we got a result
        if _NFS_SHARE_CACHE is not None:
            _NFS_SHARE_CACHE[path] = found
        return found
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


def _strip_commas(value: str) -> str:
    """Remove thousands-separator commas from a numeric string.

    rclone may output numbers like ``1,234.5`` or ``10,240`` depending
    on locale / version.  ``float("1,234.5")`` raises ValueError so we
    strip commas first.
    """
    return value.replace(",", "")


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
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,  # line-buffered — much faster than read(1)
    )
    ACTIVE_PROCESSES.append(p)

    error_tail = deque(maxlen=20)
    file_count = 0

    progress.update(
        task_id,
        description=f"[cyan]{job_name} [{phase_color}]({phase_desc})",
        completed=0.1,  # Start slightly above 0 so Rich's TimeRemainingColumn can compute ETA
        total=100.0,
        transferred="0 B",
        speed="0 B/s",
    )

    while True:
        line_raw = p.stdout.readline()
        if not line_raw:
            break

        line = line_raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        # Try rclone progress format first, then rsync
        rclone_match = rclone_progress_re.search(line)
        rsync_match = _rsync_progress_re.search(line)

        if rclone_match:
            try:
                pct = float(_strip_commas(rclone_match.group(3)))
                progress.update(
                    task_id,
                    description=(f"[cyan]{job_name} [{phase_color}]({phase_desc})"),
                    completed=pct,
                    total=100.0,
                    transferred=rclone_match.group(1),
                    speed=rclone_match.group(4) or "0 B/s",
                )
            except ValueError:
                pass  # malformed progress line — skip silently
        elif rsync_match:
            try:
                pct = float(_strip_commas(rsync_match.group(2)))
                progress.update(
                    task_id,
                    description=(f"[cyan]{job_name} [{phase_color}]({phase_desc})"),
                    completed=pct,
                    total=100.0,
                    transferred=rsync_match.group(1),
                    speed=rsync_match.group(3) or "0 B/s",
                )
            except ValueError:
                pass  # malformed progress line — skip silently
        else:
            file_count += 1
            if file_count % 1000 == 0:
                progress.update(
                    task_id,
                    description=(f"[cyan]{job_name} [{phase_color}]({phase_desc}...)"),
                    total=100.0,
                )

            error_tail.append(line)

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
    """Move data from src to dest with size+mtime verification, removing source files.

    Uses rclone move so each file is verified before being deleted
    from the temp directory. Source directories are cleaned up automatically
    (--delete-empty-src-dirs). This avoids the 2x disk space requirement of a
    full copy-then-verify approach.

    Note: verification uses size and modification-time comparison (not
    --checksum), which is sufficient for local-to-local transfers where
    the source is immediately deleted after verification.
    """
    if config is None:
        config = RcloneConfig()

    log_step(f"[Move] Starting rclone transfer for: {job_name}")
    progress.update(task_id, description=f"[cyan]{job_name} [yellow](Checking...)")

    cmd = [
        "rclone",
        "move",
        "-P",
        f"{src}/",
        f"{dest}/",
        "--transfers=" + str(config.transfers),
        "--checkers=" + str(config.checkers),
        "--buffer-size=" + config.buffer_size,
        "--use-mmap",
        "--multi-thread-streams=" + str(config.multi_thread_streams),
        "--multi-thread-cutoff=" + config.multi_thread_cutoff,
        "--no-traverse",
        "--delete-empty-src-dirs",
    ]
    return run_transfer_with_progress(
        cmd, task_id, job_name, "Moving+Verifying", "blue"
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
    if rclone_config is None:
        log_info(f"Using default rclone config for {job_name}")
        rclone_config = RcloneConfig()
    if SHUTTING_DOWN:
        log_warn(f"Skipping {job_name}: shutting down")
        return

    temp_dir = f"{job_name}-tmp"
    target_dir = job_name
    dataset = f"{pool}/{base}/{job_name}"

    nfs_path = f"/mnt/{dataset}"

    # Track whether NFS share was set up before the copy phase so we
    # don't duplicate the post-copy setup.
    nfs_setup_before_copy = False

    if is_resume:
        log_warn(f"RESUME: {temp_dir} -> {target_dir}")
        # Create NFS share BEFORE copy so clients can access data during transfer
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
        nfs_setup_before_copy = nfs_ok
    else:
        if dataset_exists(dataset):
            log_warn(f"Dataset exists -> skipping data transfer: {dataset}")
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
        log_step(f"Renaming {target_dir} -> {temp_dir}")
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

    # PHASE 1: [Move] Incremental move with size+mtime verification.
    # Source files are removed after successful verification, keeping disk
    # usage bounded throughout the transfer.
    retried = False
    success, err = run_rclone_move(
        temp_dir, target_dir, task_id, job_name, rclone_config
    )
    if not success and "0%" in err:
        log_warn(
            f"[Move] Transfer stalled, retrying with reduced concurrency "
            f"(transfers={min(rclone_config.transfers, 4)}, streams=1)"
        )
        rclone_config.multi_thread_streams = 1
        rclone_config.transfers = min(rclone_config.transfers, 4)
        time.sleep(2)
        retried = True
        success, err = run_rclone_move(
            temp_dir, target_dir, task_id, job_name, rclone_config
        )
    if not success:
        if not SHUTTING_DOWN:
            log_error(f"[Move] Failed for {job_name}.\nReason: {err}")
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
    retry_note = " (after stall recovery)" if retried else ""
    log_ok(f"{'Resume ' if is_resume else ''}Complete:{retry_note} {job_name}")

    # PHASE 4: [NFS] Create NFS share via midclt (local TrueNAS CLI)
    # Skip if we already set it up before the copy phase.
    if not nfs_setup_before_copy:
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

    # Detect system memory and disk type to tune rclone config safely.
    # Avoids OOM kernel panics on systems with limited RAM or other services.
    avail_gb = get_available_ram_gb()
    rotational = is_rotational_disk(args.path)

    workers = args.workers
    transfers = args.transfers
    checkers = args.checkers
    effective_buffer = args.buffer_size
    effective_streams = RcloneConfig.multi_thread_streams

    # Cap all concurrency based on available memory.
    # rclone allocates RAM per-concurrent-transfer for file handles,
    # metadata caches, and internal buffers — not just buffer size.
    # With 4 workers × 32 transfers = 128 simultaneous transfers,
    # each consuming ~2-5MB, that's 256-640MB per job just in transfer
    # state. On a system with ~27GB available plus ZFS ARC, this is OOM.
    if avail_gb < 16:
        workers = min(workers, 1)
        transfers = min(transfers, 4)
        checkers = min(checkers, 4)
        effective_buffer = "32M"
        log_info(
            f"Low memory ({avail_gb:.1f}GB), capping: workers=1, "
            f"transfers={transfers}, checkers={checkers}, buffer={effective_buffer}"
        )
    elif avail_gb < 32:
        workers = min(workers, 2)
        transfers = min(transfers, 8)
        checkers = min(checkers, 4)
        if effective_buffer not in ("32M", "64M"):
            effective_buffer = "64M"
        log_info(
            f"Memory-constrained ({avail_gb:.1f}GB), capping: workers={workers}, "
            f"transfers={transfers}, checkers={checkers}, buffer={effective_buffer}"
        )
    elif rotational:
        effective_streams = 1
        log_info(
            "Detected HDD pool — reducing multi-thread-streams to 1 "
            "(prevents head thrashing on spinning disks)"
        )

    log_info(
        f"Rclone config: workers={workers}, transfers={transfers}, "
        f"checkers={checkers}, buffer={effective_buffer}, "
        f"multi-thread-streams={effective_streams}, "
        f"available_ram={avail_gb:.1f}GB"
    )

    rclone_config = RcloneConfig(
        transfers=transfers,
        checkers=checkers,
        buffer_size=effective_buffer,
        multi_thread_streams=effective_streams,
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
    log_info(
        f"directory workers: {workers} (was {args.workers}, capped for memory safety)"
    )

    if total_jobs == 0:
        log_ok("Nothing to do.")
        return

    # Populate NFS share cache once at startup to avoid redundant midclt calls
    _populate_nfs_cache()

    with progress:
        global_task = progress.add_task(
            "[green]Jobs Ready",
            total=total_jobs,
            transferred="",
            speed="",
        )

        if not SHUTTING_DOWN:
            with ThreadPoolExecutor(max_workers=workers) as executor:
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
