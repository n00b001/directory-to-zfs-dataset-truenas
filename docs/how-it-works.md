# ZFS Migration Script — How It Works

## Overview

`zfs-migrate` is a deterministic, resumable script for promoting and migrating local files
into ZFS datasets on TrueNAS SCALE. It discovers subdirectories under a target mount path,
promotes each into a dedicated ZFS dataset via `zfs create`, transfers data with
[rclone](https://rclone.org/) (move + checksum verification), syncs ACL/xattr metadata with
[rsync](https://rsync.samba.org/), and exports each new dataset as an NFS share via
`midclt`.

**Key properties:**

- **Resumable** — directories suffixed with `-tmp` are treated as interrupted jobs; the script
  picks up where it left off.
- **Deterministic** — no randomness, no retries on failure, strict phase ordering.
- **Thread-safe** — concurrent dataset creation is serialized with a threading lock.
- **Graceful shutdown** — `SIGINT`/`SIGTERM` kills all in-flight transfers and exits cleanly
  without corrupting partial state.

---

## Architecture at a Glance

```
main()
 ├── Parse CLI args, validate target path
 ├── Discover subdirectories → classify as normal or resume (-tmp)
 └── ThreadPoolExecutor (workers=N)
      └── process_job(job_name, is_resume, ...)   [runs in parallel]
           ├── Dataset exists? → skip copy, create NFS share only
           ├── Rename dir to dir-tmp
           ├── zfs create pool/base/job_name
           ├── Phase 1: rclone move --checksum  (copy + verify)
           ├── Phase 2: rsync -aHAX --info=progress2  (ACL sync)
           ├── Cleanup remaining temp files
           └── Phase 4: midclt sharing.nfs.create  (NFS export)
```

---

## Step-by-Step Flow

### 1. Entry Point — `main()`

The script is invoked as a CLI tool (`uv run zfs-migrate ...`). It performs these steps:

1. **Parse arguments** — target path (required), `--workers`, `--transfers`, `--checkers`,
   `--buffer-size`. The `-y/--yes` flag is defined but not actually used in the code.
2. **Validate target** — must be a directory under `/mnt/<pool>/<base>/`. Extracts pool and
   base dataset names via `get_zfs_context()`.
3. **Chdir into target** — all subsequent operations are relative to the target directory.
4. **Discover subdirectories** — iterates `os.listdir(".")` and classifies each:

   | Classification | Condition | Action |
   |----------------|-----------|--------|
   | Skip (hidden)  | Starts with `.` | Warn + skip |
   | Skip (system)  | In `{lost+found, $RECYCLE.BIN, .Spotlight-V100, .Trashes, .fseventsd, Temporary Items}` | Warn + skip |
   | Skip (nesting) | Ends with `-tmp-tmp` | Warn — prohibited nested temp state |
   | Resume job     | Ends with `-tmp` (valid ZFS name after stripping suffix) | Submit with `is_resume=True` |
   | Normal job     | Valid ZFS name | Submit with `is_resume=False` |

5. **Deduplicate** — if a normal job name matches a resume job's base name, the normal entry
   is dropped (the `-tmp` version takes priority).
6. **Execute** — submits all jobs to a `ThreadPoolExecutor`, waits for completion via
   `concurrent.futures.as_completed()`.
7. **Final status** — if any jobs ended in `FAILED_JOBS`, logs the list and exits with code 1.
   Otherwise logs success (unless shutting down).

### 2. Per-Job Processing — `process_job()`

Each job runs in a thread pool worker. The flow depends on whether it's a resume or normal job.

#### Normal Job Path (`is_resume=False`)

```
┌─────────────────────────────────┐
│ Check SHUTTING_DOWN             │──→ skip if True
├─────────────────────────────────┤
│ dataset_exists(pool/base/name)? │──→ Yes: skip copy, create NFS share, return
├─────────────────────────────────┤
│ os.rename(name → name-tmp)      │  (make source unavailable during migration)
├─────────────────────────────────┤
│ create_dataset(pool/base/name)  │  (thread-safe via ZFS_LOCK)
├─────────────────────────────────┤
│ Phase 1: rclone move --checksum │  (copy + verify, removes verified source files)
├─────────────────────────────────┤
│ Phase 2: rsync -aHAX            │  (sync permissions, xattrs, ACLs)
├─────────────────────────────────┤
│ shutil.rmtree(name-tmp)         │  (cleanup any remaining temp files)
├─────────────────────────────────┤
│ Phase 4: midclt NFS create      │  (export as NFS share; non-fatal if it fails)
└─────────────────────────────────┘
```

#### Resume Job Path (`is_resume=True`)

Same as normal, but **skips the rename step** (the `-tmp` directory already exists on disk)
and creates the target directory with `os.makedirs()` before Phase 1. The dataset creation
still runs (idempotent — skips if it already exists).

### 3. Dataset Management

- **`dataset_exists(dataset)`** — runs `zfs list -H -o name <dataset>`. Returns True if exit code is 0.
- **`create_dataset(dataset)`** — acquires `ZFS_LOCK`, then checks again (double-checked locking)
  and runs `zfs create <dataset>`. Raises `RuntimeError` on failure.

The lock prevents race conditions when multiple threads try to create datasets concurrently.

### 4. Phase 1: Copy with Checksum Verification — `run_rclone_move()`

Uses `rclone move --checksum` to transfer files from the `-tmp` directory to the newly created
dataset mount point. Key flags:

| Flag | Purpose |
|------|---------|
| `--checksum` | Verifies by checksum, not just size/modtime |
| `--fast-list` | Optimized directory listing (fewer API calls) |
| `--no-traverse` | Doesn't list the destination before starting — faster for large dirs |
| `--delete-empty-src-dirs` | Cleans up source directories after files are moved |
| `--size-only` | Uses size as a quick pre-filter before checksum |
| `--multi-thread-streams=16` | Parallel streams per file for high-throughput remotes |
| `--multi-thread-cutoff=16M` | Files ≥ 16 MB get multi-threaded streaming |
| `--use-mmap` | Memory-maps files for I/O efficiency |
| `--transfers=N` | Concurrent file transfers (default 4) |
| `--checkers=N` | File checker threads (default 2) |
| `--buffer-size=SIZE` | Per-file in-memory buffer (default 2048M) |

**Why `move` instead of `copy`?** The `--checksum` flag ensures each file is verified before
being deleted from the source. This avoids the 2x disk space requirement of a full
copy-then-verify approach, since verification and deletion happen per-file.

### 5. Phase 2: ACL / Metadata Sync — rsync

After rclone moves the data, `rsync -aHAX --inplace --numeric-ids --info=progress2` runs
over the `-tmp` directory to sync permissions, extended attributes, and ACLs that rclone
may not preserve (especially on local-to-local transfers with complex POSIX ACLs).

The command targets any remaining files/directories in the temp dir — if rclone deleted a
file during transfer but rsync has leftover metadata from a failed partial copy, this picks
it up. `--remove-source-files` is NOT used; instead, the cleanup phase removes the entire
temp directory afterward.

### 6. Cleanup

If any files remain in the `-tmp` directory after both phases, `shutil.rmtree()` removes them
entirely. Errors during cleanup are logged as warnings — they don't fail the job.

### 7. Phase 4: NFS Share Creation — `create_nfs_share()`

Runs `midclt call sharing.nfs.create` with a JSON payload to register the new dataset mount
path as an NFS share on TrueNAS SCALE. This is **non-fatal** — if it fails (e.g., running
off-box, midclt not available), the job completes successfully but a warning is logged.

The script checks for existing shares first via `nfs_share_exists()`, which queries all NFS
shares and filters locally.

---

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| Target path not under `/mnt/<pool>/<base>/` | Exits with code 1 immediately |
| Invalid ZFS name (special chars, slashes, whitespace) | Skipped with warning |
| `-tmp-tmp` nesting | Skipped — detected as corrupted intermediate state |
| Dataset already exists | Data transfer skipped; NFS share ensured and returned |
| rclone copy fails | Job added to `FAILED_JOBS`; no NFS share attempted |
| rsync ACL sync fails | Job added to `FAILED_JOBS`; no cleanup or NFS share |
| NFS share creation fails | Warning logged; job considered successful |
| `midclt` not found (off-TrueNAS) | Returns False with error log; non-fatal |
| Temp dir cleanup fails (`OSError`) | Logged as warning; doesn't crash |
| SIGINT / SIGTERM during run | Sets `SHUTTING_DOWN=True`, kills all children, exits 130 |
| Phase failure during shutdown | Error NOT logged, job NOT added to `FAILED_JOBS` |
| Future exception in thread pool | Logged as "Job exception"; doesn't crash the main thread |

### Graceful Shutdown

When `SIGINT` or `SIGTERM` is received:

1. `SHUTTING_DOWN` flag is set to `True`.
2. All tracked child processes (`ACTIVE_PROCESSES`) are terminated.
3. `pkill -P <pid>` kills direct children of this process.
4. `pkill -f rclone` kills any orphaned rclone processes.
5. New jobs submitted after this point are skipped at the top of `process_job()`.
6. In-flight transfer failures during shutdown do **not** add to `FAILED_JOBS`.
7. The final "All complete successfully" message is suppressed.

### Process Cleanup

On exit (normal or signal), `kill_all_children()` runs via `atexit`:
- Terminates all processes in `ACTIVE_PROCESSES` list.
- Kills direct children with `pkill -P <pid>`.
- Kills any process matching "rclone" with `pkill -f rclone`.

---

## Concurrency Model

```
ThreadPoolExecutor(max_workers=args.workers)
├── Thread 1: process_job("photos", False, ...)
│   ├── zfs create tank/media/photos    (serialized by ZFS_LOCK)
│   ├── rclone move ...                 (stdout piped, progress parsed)
│   └── rsync -aHAX ...
├── Thread 2: process_job("videos", False, ...)
│   ├── zfs create tank/media/videos    (waits for ZFS_LOCK if needed)
│   ├── rclone move ...
│   └── rsync -aHAX ...
└── Thread N: ...
```

- **Per-job**: phases are sequential within a thread (copy → ACL sync → cleanup → NFS).
- **Across jobs**: multiple directories can transfer in parallel. Dataset creation is
  serialized by `ZFS_LOCK` to prevent concurrent `zfs create` races.
- **Progress tracking**: each job gets its own Rich progress task; a global "Overall Progress"
  task advances as each job completes.

---

## Configuration Reference

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `path` (positional, required) | — | Target directory (e.g., `/mnt/tank/media`) |
| `-y, --yes` | off | Auto-confirm flag (defined but unused in code) |
| `--workers N` | 4 | Thread pool size for parallel directory processing |
| `--transfers N` | 4 | rclone concurrent file transfers per job |
| `--checkers N` | 2 | rclone file checker threads per job |
| `--buffer-size SIZE` | 2048M | rclone in-memory buffer size per file |

### Module-Level State

| Variable | Type | Purpose |
|----------|------|---------|
| `LOG_FILE` | `Path` | Timestamped log file in `/tmp/` |
| `ACTIVE_PROCESSES` | `list[Popen]` | Tracked child processes for cleanup |
| `SHUTTING_DOWN` | `bool` | Global flag set on SIGINT/SIGTERM |
| `ZFS_LOCK` | `threading.Lock` | Serializes dataset creation |
| `FAILED_JOBS` | `list[str]` | Names of jobs that failed |

---

## File Structure

```
zfs-migrate/
├── zfs_migration.py          # Main script (728 lines)
│   ├── Logging system        (#84–#118)
│   ├── Signal handling       (#125–#147)
│   ├── Context validation    (#154–#194)
│   ├── Dataset management    (#197–#218)
│   ├── NFS share management  (#226–#290)
│   ├── Transfer engines      (#301–#427)
│   ├── Job execution         (#435–#569)
│   └── Main discovery        (#577–#724)
├── test_zfs_migration.py     # Test suite (2607 lines, ~100% branch coverage)
├── conftest.py               # Pytest fixtures (module state reset)
├── pyproject.toml            # Project config, CLI entry point
├── requirements.txt          # Runtime deps: rich, requests
└── .pre-commit-config.yaml   # Pre-commit: ruff fix + format
```
