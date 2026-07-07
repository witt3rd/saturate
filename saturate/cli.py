#!/usr/bin/env python3
from __future__ import annotations

import os
import time

import click
import yaml

from saturate.queue import Queue
from saturate.runner import run_turn


def get_base_dir() -> str:
    return os.environ.get("SATURATE_DIR", os.path.expanduser("~/.saturate"))


def get_queue() -> Queue:
    return Queue(base_dir=get_base_dir())


@click.group()
def main():
    """Saturate -- distributed loop execution fabric."""
    pass


@main.command()
@click.argument("spec_path", type=click.Path(exists=True))
@click.option("--output", default=None, help="Output directory for this task")
def submit(spec_path, output):
    """Submit a loop spec to the queue. Prints task_id."""
    q = get_queue()
    spec_path = os.path.abspath(spec_path)
    with open(spec_path) as f:
        spec = yaml.safe_load(f)
    task = {
        "name": spec.get("name", os.path.basename(spec_path)),
        "kind": spec.get("kind", "metric-optimization"),
        "spec_path": spec_path,
    }
    if output:
        task["output_path"] = os.path.abspath(output)
    task_id = q.post(task)
    click.echo(task_id)


@main.command("run")
@click.argument("task_id")
@click.option(
    "--loop", is_flag=True, default=False, help="Run until terminal condition"
)
def run_cmd(task_id, loop):
    """Run one turn (or loop until terminal) for a task."""
    q = get_queue()

    # Verify the task exists
    task = q.get(task_id)
    if task is None:
        click.echo(f"Error: task {task_id} not found", err=True)
        raise SystemExit(1)

    # If still pending, claim it by renaming directly
    from pathlib import Path

    base = Path(get_base_dir())
    pending_path = base / "queue" / "pending" / f"{task_id}.json"
    if pending_path.exists():
        running_path = base / "queue" / "running" / f"{task_id}.json"
        pending_path.rename(running_path)

    if loop:
        turn = 0
        while True:
            outcome = run_turn(task_id, q)
            click.echo(f"turn {turn}: {outcome}")
            turn += 1
            if outcome == "terminal":
                click.echo("Loop complete.")
                break
            # After run_turn, task is requeued (pending). Claim it again for next turn.
            pending_path = base / "queue" / "pending" / f"{task_id}.json"
            if pending_path.exists():
                running_path = base / "queue" / "running" / f"{task_id}.json"
                pending_path.rename(running_path)
    else:
        outcome = run_turn(task_id, q)
        click.echo(f"turn 0: {outcome}")


@main.command()
@click.argument("task_id", required=False)
def status(task_id):
    """Show queue counts, or turn history for a specific task."""
    q = get_queue()
    if task_id:
        task = q.get(task_id)
        if task is None:
            click.echo(f"Task {task_id} not found", err=True)
            raise SystemExit(1)
        click.echo(f"Task: {task.get('name', task_id)}")
        click.echo(f"Status: {task.get('status', 'unknown')}")
        history = q.turn_history(task_id)
        if not history:
            click.echo("No turns yet.")
        else:
            for t in history:
                click.echo(
                    f"  turn {t['turn_n']}: {t['outcome']} (value={t.get('value', '?')})"
                )
    else:
        counts = q.counts()
        click.echo(
            f"pending: {counts['pending']}  running: {counts['running']}  done: {counts['done']}"
        )


@main.command("start")
@click.option(
    "--interval",
    default=30,
    show_default=True,
    type=int,
    help="Seconds between scheduler ticks.",
)
@click.option(
    "--goals-dir",
    default="./goals",
    show_default=True,
    help="Directory of .yaml goal specs to seed into the queue.",
)
def start_cmd(interval, goals_dir):
    """Run the scheduler tick in a loop until Ctrl-C.

    On each tick the scheduler:
      1. Harvests completed tasks (writes summary.md).
      2. Reaps stale running tasks (requeues them).
      3. Seeds any new .yaml specs from --goals-dir.
      4. Dispatches eligible pending tasks as subprocesses.

    Use Ctrl-C (KeyboardInterrupt) to stop.
    """
    from saturate.queue_sqlite import SqliteQueue
    from saturate.scheduler import scheduler_tick

    base = get_base_dir()
    q = SqliteQueue(base_dir=base)
    goals_dir = os.path.abspath(goals_dir)

    click.echo(
        f"Saturate scheduler running  interval={interval}s  goals-dir={goals_dir}"
    )
    click.echo("Press Ctrl-C to stop.")

    try:
        while True:
            n = scheduler_tick(q, goals_dir)
            if n > 0:
                click.echo(f"[tick] Dispatched {n} task(s)")
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nScheduler stopped.")
