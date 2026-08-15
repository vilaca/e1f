#!/usr/bin/env bash
# Drop the transactions table so broker exports can be re-imported with the current schema.
#
# Usage:
#   scripts/drop_transactions.sh
#   scripts/drop_transactions.sh --db data/e1f.db
#
# Does not touch the prices table or etf_universe.yaml.
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

DB="data/e1f.db"

usage() {
    cat <<'EOF'
Usage:
  scripts/drop_transactions.sh [--db PATH]

Drop the SQLite transactions table (default database: data/e1f.db).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db | -d)
            [[ $# -ge 2 ]] || {
                echo "✗ Missing value for $1" >&2
                exit 1
            }
            DB="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "✗ Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "$DB" ]]; then
    echo "✗ Database not found: $DB" >&2
    exit 1
fi

if ! sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions';" | grep -q 1; then
    echo "No transactions table in $DB"
    exit 0
fi

count="$(sqlite3 "$DB" "SELECT COUNT(*) FROM transactions;")"
sqlite3 "$DB" "DROP TABLE transactions;"
echo "✓ Dropped transactions table ($count rows) from $DB"
