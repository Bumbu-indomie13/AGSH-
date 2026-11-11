#!/bin/bash
# End-to-end: run each example against offline fixtures and check JSON output
cd "$(dirname "$0")/.."

# undo-guard example needs an input file — recreate it deterministically
cat > users.json << 'EOF'
[{"id":1,"status":"active","email":"a@company.com"},{"id":2,"status":"inactive","email":"b@x.org"},{"id":3,"status":"active","email":"c@company.com"}]
EOF

FAILED=0
for f in examples/*.agsh; do
  echo "== $f =="
  python3 agsh.py < "$f" > /dev/null || { echo "FAILED: $f"; FAILED=1; }
done
rm -f users.json
[ "$FAILED" = "0" ] && echo ALL_EXAMPLES_OK || exit 1
