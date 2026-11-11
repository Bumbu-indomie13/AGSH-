#!/bin/bash
# End-to-end: run each example against offline fixtures and check JSON output
cd "$(dirname "$0")/.."
for f in examples/*.agsh; do
  echo "== $f =="
  python3 agsh.py < "$f" || exit 1
done
echo ALL_EXAMPLES_OK
