"""I3: the Cloud Tasks queue is throttled to the worker's capacity."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "backend-deploy.yml"


def test_queue_concurrency_matches_worker_max_instances():
    text = WORKFLOW.read_text(encoding="utf-8")
    update = re.search(r"gcloud tasks queues update \$TASKS_QUEUE[^\n]*\\\n([^\n]*)", text)
    assert update, "queue update step missing"
    flags = update.group(1)
    dispatches = int(re.search(r"--max-concurrent-dispatches=(\d+)", flags).group(1))
    assert "--max-dispatches-per-second=10" in flags and "--min-backoff=5s" in flags
    assert "--max-attempts" not in text  # stays at the queue default (100)
    worker = text[text.index("gcloud run deploy $WORKER_NAME"):]
    max_instances = int(re.search(r"--max-instances (\d+)", worker).group(1))
    concurrency = int(re.search(r"--concurrency (\d+)", worker).group(1))
    assert dispatches == max_instances * concurrency == 30
