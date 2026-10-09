"""Process entry point for one landing or welcome wizard job."""

import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from marketplace.landing import jobs


async def run_job(task_id: str, prompt_dir: str) -> bool:
    """Run the request stored in the job record and persist its actual outcome."""
    try:
        job = jobs.get_job(task_id)
        if job is None:
            raise ValueError("Job record not found")
        if job["status"] != "pending":
            return False
        if jobs.update_job(task_id, status="running", pid=os.getpid()) is None:
            raise RuntimeError("Could not persist running job state")

        # Import inside the failure boundary so import/preparation errors are
        # recorded just like runner errors.
        from marketplace.landing import publication, wizard

        sandbox = jobs.get_wizard_sandbox_status()
        if not sandbox.available:
            raise RuntimeError(sandbox.reason)

        target = job.get("target", "landing")
        job_type = job["type"]
        if target not in ("landing", "welcome") or job_type not in ("generate", "modify"):
            raise ValueError("Invalid wizard job type or target")
        params = job.get("request", {})
        kwargs = {
            "prompt_dir": prompt_dir,
            "timeout": params.get("timeout", job.get("timeout_seconds", 300)),
            "product_name": params.get("product_name", ""),
            "ai_system_prompt": params.get("ai_system_prompt", ""),
            "product_description": params.get("product_description", ""),
        }
        if job_type == "generate":
            kwargs.update(
                user_description=params.get("description", ""),
                style=params.get("style", "modern"),
                primary_color=params.get("primary_color", "#3B82F6"),
                secondary_color=params.get("secondary_color", "#10B981"),
                language=params.get("language", "es"),
            )
        else:
            kwargs["instructions"] = params.get("instructions", "")
        if target == "welcome":
            kwargs["language"] = params.get("language", "es")
            kwargs["avatar_path"] = params.get("avatar_path", "")
            kwargs["chat_url"] = params.get("chat_url", "/chat")
        else:
            kwargs["landing_url"] = params.get("landing_url", "")

        async with publication._landing_save_lock(job["prompt_id"], pack_id=job.get("pack_id")):
            # The sandbox mounts the full prompt/pack workspace for either
            # target. Hold exclusion and revoke both pages before runner writes.
            await publication._revoke_landing_publication(
                job["prompt_id"], pack_id=job.get("pack_id"), revoke_welcome=True,
            )
            result = getattr(wizard, f"{job_type}_{target}")(**kwargs)
            success = bool(result.get("success"))
            if success and target == "welcome":
                await publication._set_welcome_page_publication(job["prompt_id"], pack_id=job.get("pack_id"), published=True)
        updated = jobs.update_job(
            task_id,
            status="completed" if success else "failed",
            error=None if success else result.get("error", "Unknown error"),
            files_created=result.get("files_created", []),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if updated is None:
            raise RuntimeError("Could not persist final job state")
        return success
    except Exception as exc:
        jobs.update_job(
            task_id,
            status="failed",
            error=str(exc),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one landing wizard job")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("prompt_dir")
    args = parser.parse_args()
    jobs.JOBS_DIR = args.job_path.resolve().parent
    return 0 if asyncio.run(run_job(args.job_path.stem, args.prompt_dir)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
