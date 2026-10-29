# job-aggregator autostart (installed by /workspace/job-aggregator/scripts/boot.sh).
# Any login shell idempotently (re)starts cron + Ollama + the job UI in the
# background. Fast, silent, never blocks; no-op if the repo is gone.
[ -x /workspace/job-aggregator/scripts/boot-hook.sh ] && /workspace/job-aggregator/scripts/boot-hook.sh etc-profile.d </dev/null >/dev/null 2>&1
true
