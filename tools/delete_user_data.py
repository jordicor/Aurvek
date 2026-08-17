"""Administrative CLI for coordinated user-account deletion.

Runs the exact same service the HTTP endpoints use (user_deletion.delete_user_account),
so there is no duplicated deletion logic here. Intended to be run during a maintenance
window with the web app and other writers stopped.

Usage:
    python tools/delete_user_data.py --username alice --dry-run
    python tools/delete_user_data.py --user-id 126 --execute --confirm alice

Exactly one target (--user-id / --username) and exactly one mode (--dry-run /
--execute) are required. --execute additionally requires --confirm <username> that
matches the resolved target username (case-insensitive). Re-running --execute on an
already-deleted user is safe.

Output is ASCII-only (Windows cp1252 terminals).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a plain script: python tools/delete_user_data.py ...
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from user_deletion import DeletionResult, delete_user_account  # noqa: E402


def _print_report(result: DeletionResult, *, header: str) -> None:
    print("=" * 68)
    print(header)
    print("=" * 68)
    print(f"  user_id:  {result.user_id}")
    print(f"  username: {result.username}")
    print(f"  status:   {result.status}")
    if result.summary:
        print(f"  summary:  {result.summary}")

    if result.status == "blocked":
        print("  guards:   BLOCKED -> " + ", ".join(result.reason_codes))
    elif result.status != "not_found":
        print("  guards:   PASS")

    if result.blocking_tables:
        print("  blocking rows:")
        for block in result.blocking_tables:
            print(f"    - {block['table']}: {block['reason']} ({block['count']})")

    if result.table_counts:
        print("  row counts:")
        for label, count in result.table_counts.items():
            print(f"    - {label}: {count}")

    print(f"  conversations: {len(result.conversation_ids)}")
    print(f"  transactions archived: {result.archived_transaction_count}")
    print(f"  atagia user id: {result.atagia_user_id}")
    print(f"  atagia links:   {result.atagia_link_count}")
    if result.atagia_report is not None:
        print(f"  atagia erase report: {result.atagia_report}")

    if result.elevenlabs_session_ids:
        print("  ElevenLabs remote conversation ids (auto-purged post-commit):")
        for session_id in result.elevenlabs_session_ids:
            print(f"    - {session_id}")
    else:
        print("  ElevenLabs remote conversation ids: none")

    print("  file paths that would be / were removed:")
    for path in result.file_paths:
        print(f"    - {path}")

    if result.cleanup_errors:
        print("  CLEANUP ERRORS (manual retry required):")
        for entry in result.cleanup_errors:
            if isinstance(entry, dict):
                print(
                    "    - [%s] %s: %s"
                    % (
                        entry.get("kind", "unknown"),
                        entry.get("target", ""),
                        entry.get("error", ""),
                    )
                )
            else:
                print(f"    - {entry}")
    print("=" * 68)


async def _run(args: argparse.Namespace) -> int:
    # Resolve, evaluate guards, and capture without mutating anything.
    preview = await delete_user_account(
        user_id=args.user_id,
        username=args.username,
        dry_run=True,
    )

    if args.dry_run:
        _print_report(preview, header="DRY RUN (no changes made)")
        if preview.status == "not_found":
            print("User not found; nothing to delete.")
        return 0

    # --execute
    if preview.status == "not_found":
        _print_report(preview, header="EXECUTE (nothing to do)")
        print("User not found or already deleted; nothing to do (idempotent).")
        return 0

    if preview.status == "blocked":
        _print_report(preview, header="EXECUTE BLOCKED")
        print("Deletion is blocked by a guard; resolve it manually before retrying.")
        return 1

    if args.confirm is None or args.confirm.strip().lower() != (preview.username or "").lower():
        print(
            "Refusing to execute: --confirm must match the target username "
            f"'{preview.username}'."
        )
        return 2

    result = await delete_user_account(user_id=preview.user_id, dry_run=False)
    _print_report(result, header="EXECUTE RESULT")

    if result.status == "completed":
        return 0
    if result.status == "completed_with_cleanup_errors":
        print(
            "Deletion committed, but some cleanup steps (file removal or remote "
            "ElevenLabs purge) failed. Retry cleanup."
        )
        return 1
    if result.status == "atagia_unreachable":
        print("Atagia is unreachable; no local changes were made. Retry later.")
        return 1
    if result.status == "blocked":
        print("Deletion became blocked between preview and execute; nothing was deleted.")
        return 1
    print(f"Unexpected status: {result.status}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete all data for one Aurvek user account (admin maintenance tool).",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", type=int, help="Numeric id of the target user.")
    target.add_argument("--username", help="Username of the target user.")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report guards, counts, external ids, and file paths without changing anything.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform the deletion (requires --confirm <username>).",
    )
    parser.add_argument(
        "--confirm",
        help="Must equal the target username to authorize --execute.",
    )

    args = parser.parse_args()

    if args.execute and not args.confirm:
        parser.error("--execute requires --confirm <username> matching the target user.")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
