#!/bin/bash
TIMESTAMP=$(date +%s)
echo "=== AgentWorld Deploy Build $TIMESTAMP ==="
cp /var/www/agentworld/v2.html /var/www/agentworld/v2.html.bak.$TIMESTAMP
python3 /root/agentworld/inject_ts.py $TIMESTAMP
systemctl restart agentworld
sleep 2
systemctl is-active agentworld && echo "Service OK" || echo "Service FAILED"
nginx -t 2>/dev/null && nginx -s reload && echo "nginx reloaded"
ls -t /var/www/agentworld/v2.html.bak.* 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null
echo "DEPLOY DONE build v$TIMESTAMP"
echo "https://agentworld.me/v2.html"
