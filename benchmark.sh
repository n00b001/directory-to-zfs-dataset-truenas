#!/usr/bin/env bash
# Full rclone move benchmark sweep — tmpfs → NFS (real network I/O)
set -uo pipefail

BASE="/home/alex/truenas-mount/shuttle/alex-home/directory-to-zfs-dataset-truenas"
SRC="/tmp/source"
DST="$BASE/dest"
SIZE_MB=10240
RESULTS_FILE="/tmp/bench_results.txt"

create_test_data() {
    find "$SRC" -type f -delete 2>/dev/null || true
    dd if=/dev/urandom of="$SRC/testfile.bin" bs=1M count=$SIZE_MB status=none
}

clean_dest() {
    find "$DST" -type f -delete 2>/dev/null || true
}

run_one() {
    local label="$1" t="$2" c="$3" buf="$4" mt="$5" cutoff="$6"
    create_test_data
    clean_dest

    local start end elapsed_s speed_peak avg_speed
    start=$(date +%s%N)

    # Capture the last non-zero transfer line for peak speed
    rclone move -P "$SRC/" "$DST/" \
        --transfers="$t" --checkers="$c" --buffer-size="$buf" \
        --multi-thread-streams="$mt" --multi-thread-cutoff="$cutoff" \
        --no-traverse --delete-empty-src-dirs --fast-list 2>&1 | tee /tmp/rclone_run.txt &
    local rpid=$!
    wait $rpid

    end=$(date +%s%N)
    elapsed_s=$(echo "scale=2; ($end - $start) / 1000000000" | bc)

    # Peak speed from the transfer phase (exclude cleanup speeds near 0)
    peak=$(grep -oE '[0-9.]+ MiB/s' /tmp/rclone_run.txt | grep -v 'MiB$' | sort -t' ' -k1 -rn | head -1)
    # Average = total size / transfer-only time (exclude ~65s check phase)
    # Transfer started after "Transferring:" line, ended at 100%
    local transfer_start_line transfer_end_line
    transfer_start_line=$(grep -n "Checking:" /tmp/rclone_run.txt | head -1 | cut -d: -f1)
    transfer_end_line=$(grep -n "Checks:.*2 / 2, 100%" /tmp/rclone_run.txt | tail -1 | cut -d: -f1)

    echo "${label}: t=$t c=$c buf=$buf mt_streams=$mt cutoff=$cutoff" > "$RESULTS_FILE"
    echo "  Total time: ${elapsed_s}s | Peak: ${peak}" >> "$RESULTS_FILE"
    echo "  Config line:" >> "$RESULTS_FILE"
    echo "    --transfers=${t} --checkers=${c} --buffer-size=${buf} --multi-thread-streams=${mt} --multi-thread-cutoff=${cutoff}" >> "$RESULTS_FILE"
    echo "" >> "$RESULTS_FILE"
    cat "$RESULTS_FILE"
}

echo "=== Rclone Move Benchmark Sweep on NFS ==="
echo "Test file: ${SIZE_MB}MB random data (tmpfs → NFS)"
echo "CPU: i7-6700K (4C/8T), RAM: 32GB"
echo ""

# Phase 1: Vary multi-thread-streams (the big lever for single-file throughput)
echo "--- Phase 1: Multi-thread-streams sweep ---"
run_one "S1" 64 32 "512M" 1 4G
run_one "S2" 64 32 "512M" 4 4G
run_one "S3" 64 32 "512M" 8 4G
run_one "S4" 64 32 "512M" 16 4G
run_one "S5" 64 32 "512M" 32 4G

# Phase 2: Vary buffer size with best stream count from phase 1
echo "--- Phase 2: Buffer-size sweep ---"
run_one "B1" 64 32 "256M" 8 4G
run_one "B2" 64 32 "512M" 8 4G
run_one "B3" 64 32 "1024M" 8 4G
run_one "B4" 64 32 "2048M" 8 4G

# Phase 3: Vary transfers (parallel file count — matters for many-file workloads)
echo "--- Phase 3: Transfers sweep ---"
run_one "T1" 16 32 "1024M" 8 4G
run_one "T2" 32 32 "1024M" 8 4G
run_one "T3" 64 32 "1024M" 8 4G
run_one "T4" 128 32 "1024M" 8 4G

# Phase 4: Top combos — full config
echo "--- Phase 4: Full configs ---"
run_one "F1" 32 32 "512M" 8 4G
run_one "F2" 64 32 "1024M" 8 4G
run_one "F3" 64 32 "1024M" 16 4G
run_one "F4" 64 32 "2048M" 8 4G
run_one "F5" 128 32 "1024M" 8 4G

echo "=== FULL RESULTS ==="
cat "$RESULTS_FILE"
echo ""
echo "Cleaning up..."
find "$SRC" -type f -delete 2>/dev/null || true
find "$DST" -type f -delete 2>/dev/null || true
