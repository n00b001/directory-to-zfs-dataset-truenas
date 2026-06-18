#!/usr/bin/env bash
set -euo pipefail

BASE="/home/alex/truenas-mount/shuttle/alex-home/directory-to-zfs-dataset-truenas"
SRC="$BASE/source"
DST="$BASE/dest"
SIZE_MB=10240

create_test_data() {
    find "$SRC" -type f -delete 2>/dev/null || true
    dd if=/dev/urandom of="$SRC/testfile.bin" bs=1M count=$SIZE_MB status=none
    echo "  Created $(du -sh "$SRC" | cut -f1) in source"
}

clean_dest() {
    find "$DST" -type f -delete 2>/dev/null || true
}

run_one() {
    local label="$1"; shift
    create_test_data
    clean_dest

    local start end elapsed_s speed
    start=$(date +%s%N)

    rclone move -P "$SRC/" "$DST/" "$@" --no-traverse --delete-empty-src-dirs 2>&1 | tail -3

    end=$(date +%s%N)
    elapsed_s=$(echo "scale=2; ($end - $start) / 1000000000" | bc)
    speed=$(echo "scale=1; $SIZE_MB / $elapsed_s" | bc)
    echo "  => ${SIZE_MB}MB in ${elapsed_s}s = ${speed} MiB/s avg throughput"
    echo ""
}

echo "=== Rclone Move Benchmark on NFS ==="
echo "Test file: ${SIZE_MB}MB random data"
echo ""

run_one "C1: t=32 c=16 buf=512M mt8 cutoff4G" \
    --transfers=32 --checkers=16 --buffer-size=512M --multi-thread-streams=8 --multi-thread-cutoff=4G

run_one "C2: t=64 c=32 buf=512M mt16 cutoff4G" \
    --transfers=64 --checkers=32 --buffer-size=512M --multi-thread-streams=16 --multi-thread-cutoff=4G

run_one "C3: t=16 c=8 buf=1024M mt4 cutoff4G" \
    --transfers=16 --checkers=8 --buffer-size=1024M --multi-thread-streams=4 --multi-thread-cutoff=4G

run_one "C4: t=64 c=32 buf=1024M mt16 cutoff16M" \
    --transfers=64 --checkers=32 --buffer-size=1024M --multi-thread-streams=16 --multi-thread-cutoff=16M

run_one "C5: t=8 c=4 buf=256M mt2 cutoff4G" \
    --transfers=8 --checkers=4 --buffer-size=256M --multi-thread-streams=2 --multi-thread-cutoff=4G

run_one "C6: t=32 c=16 buf=1024M mt8 cutoff16M" \
    --transfers=32 --checkers=16 --buffer-size=1024M --multi-thread-streams=8 --multi-thread-cutoff=16M

run_one "C7: t=128 c=32 buf=512M mt16 cutoff4G" \
    --transfers=128 --checkers=32 --buffer-size=512M --multi-thread-streams=16 --multi-thread-cutoff=4G

run_one "C8: t=32 c=16 buf=512M mt1 cutoff4G" \
    --transfers=32 --checkers=16 --buffer-size=512M --multi-thread-streams=1 --multi-thread-cutoff=4G

echo "=== RESULTS ==="
echo "Cleaning up..."
find "$SRC" -type f -delete 2>/dev/null || true
find "$DST" -type f -delete 2>/dev/null || true
