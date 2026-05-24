# ZFS Migration Tool

Deterministic, resumable ZFS dataset promotion and migration script for **TrueNAS SCALE**.

Migrate directories to individual ZFS datasets with automatic NFS share export — all via `midclt` running locally on the TrueNAS host.

## Features

- **ZFS dataset creation** — Creates one dataset per directory under the target path
- **NFS share export** — Automatically exports each dataset as an NFS share via `midclt`
- **Resumable transfers** — Re-run after interruption; picks up where it left off
- **Three-phase verification**:
  1. **Copy** — `rclone move` with checksum verification (source files removed after verify)
  2. **ACL sync** — `rsync -aHAX` for permissions, extended attributes, and ACLs
  3. **Checksum** — Final read-only `rclone check` integrity pass
- **Parallel workers** — Configurable thread pool for concurrent directory migration
- **Live progress** — Rich TUI with transfer speed, ETA, and per-job status
- **Graceful shutdown** — Ctrl+C stops workers cleanly, preserves state for resume
- **Existing dataset handling** — Skips data transfer if dataset exists, ensures NFS share is created

## Requirements

- **TrueNAS SCALE** (26.0+) — script runs locally via SSH
- **uv** — Python package manager (<https://github.com/astral-sh/uv>)
- **rclone** — for data transfer and checksum verification
- **rsync** — for ACL/xattr metadata sync
- **midclt** — TrueNAS middleware CLI (pre-installed on TrueNAS SCALE)

## Installation

```bash
# Clone the repo
git clone git@github.com:n00b001/directory-to-zfs-dataset-truenas.git
cd directory-to-zfs-dataset-truenas

# Install dependencies (creates .venv automatically)
uv sync
```

## Usage

```bash
# Migrate all directories under /mnt/tank/media to ZFS datasets + NFS shares
uv run zfs-migrate /mnt/tank/media

# Auto-confirm, 8 parallel workers
uv run zfs-migrate /mnt/tank/media -y --workers 8

# Tune rclone performance
uv run zfs-migrate /mnt/tank/media --transfers 8 --checkers 4 --buffer-size 1G
```

### Command-line Options

| Option | Default | Description |
|--------|---------|-------------|
| `path` | (required) | Target directory (e.g., `/mnt/tank/media`) |
| `-y`, `--yes` | off | Auto-confirm, skip prompt |
| `--workers N` | 4 | Parallel directory workers |
| `--transfers N` | 4 | rclone concurrent transfers per job |
| `--checkers N` | 2 | rclone file checker threads per job |
| `--buffer-size SIZE` | 256M | rclone in-memory buffer per file |

### How It Works

1. Scans the target directory for subdirectories
2. For each directory:
   - Renames to `<name>-tmp` (if not already in-progress)
   - Creates ZFS dataset `pool/base/<name>`
   - Copies data with `rclone move --checksum` (verifies + removes source)
   - Syncs ACLs/xattrs with `rsync -aHAX`
   - Verifies integrity with `rclone check`
   - Creates NFS share via `midclt`
3. Skips hidden folders, system folders (`lost+found`, `$RECYCLE.BIN`, etc.)

### Resume

If interrupted, re-run with the same path. Directories ending in `-tmp` are detected and resumed. Datasets that already exist are skipped (NFS share is still ensured).

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev,test]"

# Run tests (parallel with xdist, coverage enabled)
uv run pytest

# Run pre-commit hooks (ruff check + format)
uv run pre-commit run --all-files
```

## Project Structure

```
├── zfs_migration.py      # Main script
├── test_zfs_migration.py # Tests (pytest)
├── conftest.py           # Test fixtures
├── pyproject.toml        # Project metadata, dependencies
└── README.md
```

## Roadmap

- [ ] Network-restricted NFS shares (`--network` flag)
- [ ] SMB share export option
- [ ] Dataset property templates (compression, recordsize, atime)
- [ ] Dry-run mode (`--dry-run`)
- [ ] JSON/CSV migration manifest output
- [ ] Pre-transfer disk space validation
- [ ] Email/Slack notifications on completion
- [ ] GUI dashboard for monitoring

## License

MIT