# probe: box health, which code the daemon runs, where its log is
date -u; uptime
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader
free -g | head -2; df -h /kaggle/working | tail -1
echo "== procs"; ps -eo pid,etime,pcpu,rss,args | grep -E '[v]llm serve|[r]esearch|[b]ox_agent|[k]ernel' | cut -c1-200
echo "== code"; git -C /kaggle/working/sabha log --oneline -2; git -C /kaggle/working/sabha status --short | head
echo "== logs"; ls -lt --time-style=+%H:%M /kaggle/working/*.log /kaggle/working/logs/ /tmp/*.log 2>/dev/null | head -20
for f in $(ls -t /kaggle/working/*research*.log /kaggle/working/logs/*research*.log /tmp/*research*.log 2>/dev/null | head -1); do echo "== tail $f"; tail -40 "$f"; done
echo "== KeyError traces"; grep -rl "KeyError: 'status'" /kaggle/working/*.log /kaggle/working/logs /tmp/*.log 2>/dev/null | head -3
