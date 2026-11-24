from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ...services import reconcile


class Command(BaseCommand):
    help = (
        "Verify payments that were paid but never verified (buyer closed the browser), re-check stale "
        "pending payments, re-send payment_verified when a receiver failed and prune old webhook events. "
        "Schedule it every few minutes (cron, Celery beat, systemd timer)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--older-than",
            type=int,
            default=None,
            metavar="MINUTES",
            help="Only re-check pending payments older than this (default RECONCILE_AFTER_MINUTES).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Maximum pending payments to re-check.")

    def handle(self, *args: Any, **options: Any) -> None:
        stats = reconcile(older_than_minutes=options["older_than"], limit=options["limit"])
        self.stdout.write(" ".join(f"{name}={value}" for name, value in stats.items()))
        if stats["errors"]:
            self.stderr.write(self.style.WARNING("Some payments could not be processed; they will be retried."))
