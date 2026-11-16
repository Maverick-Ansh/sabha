# Ansh stopped mission 001 early (2026-11-16 ~08:00 UTC): stop the daemon, save everything to git, stop the models.
date -u
pkill -f '[w]hile true; do SABHA_S1' ; echo "relaunch loop stopped"
pkill -TERM -f '[s]abha[.]research[.]run' ; for i in $(seq 1 20); do pgrep -f '[s]abha[.]research[.]run' >/dev/null || break; sleep 1; done
pkill -KILL -f '[s]abha[.]research[.]run' ; echo "daemon stopped"
pkill -f '[s]abha[.]research[.]index' ; echo "index builder stopped"
cd /kaggle/working/sabha-research
git add -A
git commit -q -m "box: final save, mission 001 stopped early by Ansh $(date -u +%Y-%m-%dT%H:%M:%SZ)" || echo "nothing new to commit"
git fetch -q origin && git merge -q --no-edit origin/main
timeout 120 git push -q origin HEAD:main && echo "FINAL_PUSH_OK $(git log --oneline -1)"
cd /kaggle/working/sabha
bash sabha/research/serve.sh stop reader; bash sabha/research/serve.sh stop deep
sleep 5; nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
echo "DONE: safe to Stop Session in Kaggle"
