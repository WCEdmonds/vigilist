"""Upload local files to a production and start ingest (ops CLI).

The command-line twin of the Ingest Wizard: same storage layout
(``productions/{id}/raw/loads/{load_id}/...``), same create/process code paths,
so a CLI load is indistinguishable from a browser load. Use it for uploads too
large or too numerous for the browser.

Usage (from backend/):

    venv/Scripts/python.exe -m scripts.upload_ingest \\
        --production "Vote Thiru" --owner wcedmonds28@gmail.com \\
        --custodian will@votethiru.com \\
        [--create] [--source-type collection] [--wait] \\
        [--env-from-cloud-run vigilist-api --gcloud-account you@example.com] \\
        PATH [PATH ...]

PATH may be a file or a directory (uploaded recursively, paths kept relative to
the directory). ``--env-from-cloud-run`` loads the deployed service's VIGILIST_*
settings into this process's environment (in memory only, never written to
disk) so the CLI talks to the same database, bucket and Cloud Tasks queue.
Storage and Cloud Tasks calls use Application Default Credentials.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid


def _load_cloud_run_env(service: str, region: str, project: str, account: str | None) -> None:
    cmd = [
        "gcloud", "run", "services", "describe", service,
        "--region", region, "--project", project, "--format", "json",
    ]
    if account:
        cmd += ["--account", account]
    out = subprocess.run(cmd, capture_output=True, text=True, shell=(os.name == "nt"))
    if out.returncode != 0:
        raise SystemExit(f"gcloud run services describe failed:\n{out.stderr.strip()}")
    containers = json.loads(out.stdout)["spec"]["template"]["spec"]["containers"]
    loaded = 0
    for var in containers[0].get("env", []):
        if var.get("name", "").startswith("VIGILIST_") and "value" in var:
            os.environ[var["name"]] = var["value"]
            loaded += 1
    print(f"Loaded {loaded} VIGILIST_* settings from Cloud Run service {service}")


def _collect(paths: list[str]) -> list[tuple[str, str]]:
    """(local_path, relative_path) for every file under ``paths``, sorted."""
    files: list[tuple[str, str]] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for name in names:
                    local = os.path.join(root, name)
                    files.append((local, os.path.relpath(local, p).replace(os.sep, "/")))
        elif os.path.isfile(p):
            files.append((p, os.path.basename(p)))
        else:
            raise SystemExit(f"Not found: {p}")
    return sorted(files, key=lambda f: f[1])


async def main() -> int:
    p = argparse.ArgumentParser(description="Upload files to a production and start ingest")
    p.add_argument("paths", nargs="+", help="files or directories to upload")
    p.add_argument("--production", required=True, help="production name")
    p.add_argument("--owner", required=True, help="email of the production owner (an existing Vigilist user)")
    p.add_argument("--create", action="store_true", help="create the production if it does not exist")
    p.add_argument("--custodian", default="", help="custodian stamped on every document")
    p.add_argument("--source-party", default="", help="source party label (optional)")
    p.add_argument("--source-type", default="collection", choices=["collection", "received"])
    p.add_argument("--wait", action="store_true", help="poll until the ingest job finishes")
    p.add_argument("--dry-run", action="store_true", help="list what would be uploaded and stop")
    p.add_argument("--env-from-cloud-run", metavar="SERVICE", help="load settings from this Cloud Run service")
    p.add_argument("--region", default="us-central1")
    p.add_argument("--project", default="ediscover")
    p.add_argument("--gcloud-account", default=None, help="gcloud account for --env-from-cloud-run")
    args = p.parse_args()

    files = _collect(args.paths)
    total_bytes = sum(os.path.getsize(f[0]) for f in files)
    print(f"{len(files)} file(s), {total_bytes / 2**20:,.1f} MB")
    if args.dry_run:
        for local, rel in files:
            print(f"  {rel}  ({os.path.getsize(local) / 2**20:,.1f} MB)")
        return 0
    if not files:
        print("Nothing to upload.", file=sys.stderr)
        return 1

    if args.env_from_cloud_run:
        _load_cloud_run_env(args.env_from_cloud_run, args.region, args.project, args.gcloud_account)

    # Import after the environment is in place: settings are read at import.
    import firebase_admin
    from fastapi import BackgroundTasks, HTTPException
    from sqlalchemy import select

    from app.config import settings
    from app.database import async_session
    from app.models import IngestJob, Production, User
    from app.routers.ingest import create_production_for_ingest, start_processing
    from app.services.ingest import sources_attempted
    from app.services.storage import upload_file

    from app.services import tasks as task_service

    if not task_service.is_configured():
        # Without Cloud Tasks, start_processing falls back to an in-process
        # BackgroundTask, which never runs in a CLI. Refuse rather than strand
        # the upload.
        print("ERROR: Cloud Tasks is not configured; use --env-from-cloud-run", file=sys.stderr)
        return 1

    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id or args.project})

    async with async_session() as db:
        user = (await db.execute(select(User).where(User.email == args.owner))).scalar_one_or_none()
        if user is None:
            print(f"ERROR: no Vigilist user with email {args.owner} (they must sign in once first)",
                  file=sys.stderr)
            return 1

        production = (await db.execute(
            select(Production).where(Production.name == args.production)
        )).scalar_one_or_none()
        if production is None:
            if not args.create:
                print(f"ERROR: production {args.production!r} not found (pass --create)", file=sys.stderr)
                return 1
            created = await create_production_for_ingest({"production_name": args.production}, db, user)
            production = await db.get(Production, created["production_id"])
            print(f"Created production #{production.id}: {production.name}")
        else:
            print(f"Using production #{production.id}: {production.name}")
        production_id = production.id

    load_id = uuid.uuid4().hex[:8]
    prefix = f"productions/{production_id}/raw/loads/{load_id}/"
    done_bytes = 0
    for i, (local, rel) in enumerate(files, start=1):
        size = os.path.getsize(local)
        print(f"[{i}/{len(files)}] uploading {rel} ({size / 2**20:,.1f} MB)", flush=True)
        await asyncio.to_thread(upload_file, local, prefix + rel)
        done_bytes += size
    print(f"Uploaded {done_bytes / 2**20:,.1f} MB to {prefix}")

    body = {
        "production_id": production_id,
        "source_format": "native",
        "custodian": args.custodian,
        "source_party": args.source_party,
        "source_type": args.source_type,
        "load_id": load_id,
    }
    async with async_session() as db:
        user = await db.get(User, user.id)
        try:
            job_out = await start_processing(body, BackgroundTasks(), db, user)
        except HTTPException as e:
            print(f"ERROR starting ingest: {e.status_code} {e.detail}", file=sys.stderr)
            return 1
    job_id = job_out.id
    print(f"Ingest job {job_id}: {job_out.total_files} file(s) queued")

    if not args.wait:
        return 0
    last = None
    while True:
        await asyncio.sleep(15)
        async with async_session() as db:
            job = await db.get(IngestJob, job_id)
            line = (f"{job.status} · {sources_attempted(job)}/{job.total_files} files · "
                    f"{job.processed_files} documents · {job.skipped_files} skipped")
            if line != last:
                print(line, flush=True)
                last = line
            if job.status in ("complete", "failed"):
                for err in job.errors or []:
                    print(f"  ! {err}")
                return 0 if job.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
