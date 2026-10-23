#!/bin/bash
# Reset the staging demo: remove the demo cooperative, clear staging rate limits, seed it again
# (with officers), and build the frontend against staging into frontend/dist-stage.
# Needs: bench --site coopvault.test serve --port 8100 --noreload, and dev/tests/fake_paystack.py 8899
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
BENCH=/home/ubuntu/start-payment/backend
STATE=$HERE/tests/.state/demo_stage.json
PASSWORD='CoopDemo#2026'
cd $BENCH
if [ -f "$STATE" ]; then
  CO=$(python3 -c "import json;print(json.load(open('$STATE'))['coop'])")
  (cat $HERE/tests/cleanup_coop.py; echo; echo "run('$CO')") | bench --site coopvault.test console >/dev/null 2>&1 || true
fi
DB=$(python3 -c "import json;print(json.load(open('sites/coopvault.test/site_config.json'))['db_name'])")
redis-cli -p 13000 --scan --pattern "$DB|rl:*" | xargs -r -d '\n' redis-cli -p 13000 del >/dev/null
python3 $HERE/tests/seed_v3.py stage "$PASSWORD" | tail -1
python3 $HERE/tests/seed_officers.py stage "$PASSWORD" | tail -1
python3 $HERE/tests/seed_phase5.py stage "$PASSWORD" | tail -1
python3 $HERE/tests/seed_platform.py stage "$PASSWORD" | tail -1
cd /home/ubuntu/start-payment/frontend && VITE_API_URL=http://127.0.0.1:8100 npx vite build --outDir dist-stage --emptyOutDir >/dev/null 2>&1 && echo "built frontend/dist-stage"
