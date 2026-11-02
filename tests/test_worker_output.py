"""Real worker death with finite synthetic children; no GROMACS or user services."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = r'''
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from app.execution import identity_matches
from app.gromacs import Step
from app.job_store import JobStore

# Reap this harness's orphan rather than leaving it to the host's init process.
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
root, outcome = Path(sys.argv[1]), sys.argv[2]
store = JobStore(root)
emitter = """
import os, pathlib, signal, time
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
root = pathlib.Path('.')
deadline = time.monotonic() + 8
index = 0
while not (root / 'finish').exists() and time.monotonic() < deadline:
    index += 1
    os.write(1, ('step %d time %.2f\\n' % (index, index * 0.002)).encode())
    (root / 'count').write_text(str(index))
    time.sleep(0.02)
if (root / 'fail').exists():
    os._exit(3)
(root / 'md.log').write_text('Finished mdrun\\n')
os.write(1, 'Performance: 12.500\\nfinal output 汉字'.encode())
"""
step = Step('synthetic mdrun', [sys.executable, '-u', '-c', emitter, 'mdrun', '-deffnm', 'md'])
steps = [step]
if outcome == 'step_window':
    second = "from pathlib import Path; Path('second_started').touch(); Path('second.log').write_text('Finished mdrun')"
    steps.append(Step('second mdrun', [sys.executable, '-c', second, 'mdrun', '-deffnm', 'second']))
job = store.create('durable output', {'workflow': 'custom'}, [], steps)
store.start(job['id'], steps)
workdir = store.job_dir(job['id'])
(workdir / 'md.log').write_text('Finished mdrun from an earlier invocation\n')
worker_code = """
import sys, time
from pathlib import Path
from app import execution, reproducibility
from app.job_store import JobStore
reproducibility.execution_software = lambda: {}
reproducibility.detect_gromacs = lambda _: {}
execution.MIN_FREE_DISK_BYTES = 0
engine = execution.WorkerEngine(JobStore(Path(sys.argv[1])), 'isolated-worker')
if sys.argv[2] == 'initial' and sys.argv[3] == 'commit_window':
    tail = engine._tail_output
    def pause_after_drain(*args, **kwargs):
        tail(*args, **kwargs)
        Path(sys.argv[1], 'drained').touch()
        time.sleep(8)
    engine._tail_output = pause_after_drain
if sys.argv[2] == 'initial' and sys.argv[3] == 'step_window':
    cleanup = engine._cleanup_consumed_output
    def pause_before_next_spawn(meta):
        if meta.get('step_index') == 2:
            Path(sys.argv[1], 'next-step-published').touch()
            time.sleep(8)
        cleanup(meta)
    engine._cleanup_consumed_output = pause_before_next_spawn
if sys.argv[2] == 'restart':
    engine.recover_orphaned_jobs()
engine.run_once()
"""
workers, identity, reaped = [], None, False

def wait_until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Synthetic worker did not reach expected state')

def start(mode):
    proc = subprocess.Popen([sys.executable, '-c', worker_code, str(root), mode, outcome], stdout=subprocess.DEVNULL)
    workers.append(proc)
    return proc

try:
    worker = start('initial')
    wait_until(lambda: store.get(job['id']).get('process_pid') and 'step ' in store.log_path(job['id']).read_text())
    identity = store.get(job['id'])
    if outcome in {'commit_window', 'step_window'}:
        (workdir / 'finish').touch()
        wait_until(lambda: (root / ('drained' if outcome == 'commit_window' else 'next-step-published')).exists())
    worker.kill()
    assert worker.wait(timeout=3) == -signal.SIGKILL
    before = (workdir / identity['process_output']['path']).stat().st_size
    if outcome in {'commit_window', 'step_window'}:
        after_crash_line = 'final output 汉字'
    else:
        wait_until(lambda: (workdir / identity['process_output']['path']).stat().st_size > before + 60)
        after_crash_line = (workdir / identity['process_output']['path']).read_bytes()[before:].splitlines()[1].decode()
        assert identity_matches(identity, workdir), 'Emitting child must survive the worker reader exiting'
    if outcome.startswith('offline_') or outcome in {'commit_window', 'step_window'}:
        if outcome == 'offline_failed':
            (workdir / 'fail').touch()
        (workdir / 'finish').touch()
        try:
            waited, status = os.waitpid(identity['process_pid'], 0)
            reaped = waited == identity['process_pid']
        except ChildProcessError:
            # Popen.poll() already reaped the successful child before the
            # deliberately paused state publication/next-step transition.
            assert outcome in {'commit_window', 'step_window'}
            assert not identity_matches(identity, workdir)
            status, reaped = 0, True
    elif outcome == 'cancel_before_restart':
        store.cancel(job['id'])
    restarted = start('restart')
    if outcome not in {'cancel_before_restart', 'step_window'}:
        wait_until(lambda: ('recovering output' if outcome.startswith('offline_') or outcome == 'commit_window' else 're-adopted verified process') in store.log_path(job['id']).read_text())
    if outcome == 'completed':
        (workdir / 'finish').touch()
    elif outcome == 'cancelled':
        store.cancel(job['id'])
    assert restarted.wait(timeout=5) == 0
    if not reaped:
        waited, status = os.waitpid(identity['process_pid'], 0)
        reaped = waited == identity['process_pid']
    final = store.get(job['id'])
    expected = {'offline_completed': 'completed', 'offline_failed': 'interrupted', 'cancel_before_restart': 'cancelled', 'commit_window': 'completed', 'step_window': 'interrupted'}.get(outcome, outcome)
    assert final['status'] == expected, final
    assert after_crash_line in store.log_path(job['id']).read_text()
    if expected == 'completed':
        assert os.waitstatus_to_exitcode(status) == 0
        assert final['performance_ns_per_day'] == 12.5
        assert final['progress_percent'] == 100
        assert 'final output 汉字' in store.log_path(job['id']).read_text()
    elif expected == 'cancelled':
        assert os.waitstatus_to_exitcode(status) == -signal.SIGTERM
    elif outcome == 'step_window':
        assert os.waitstatus_to_exitcode(status) == 0
        assert not (workdir / 'second_started').exists()
    else:
        assert os.waitstatus_to_exitcode(status) == 3
    assert not list(workdir.glob('.worker-output-*.log'))
    print(json.dumps({'status': final['status'], 'reaped': reaped}))
finally:
    for worker in workers:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
    if identity is None:
        identity = store.get(job['id'])
    if identity.get('process_pid') and not reaped:
        if identity_matches(identity, workdir):
            os.killpg(identity['process_pgid'], signal.SIGKILL)
        try:
            os.waitpid(identity['process_pid'], 0)
        except ChildProcessError:
            pass
'''


@pytest.mark.skipif(sys.platform != "linux", reason="Uses Linux process identity and isolated child subreaping")
@pytest.mark.parametrize(
    "outcome", ["completed", "cancelled", "offline_completed", "offline_failed", "cancel_before_restart", "commit_window", "step_window"]
)
def test_emitting_calculation_survives_worker_death_and_can_be_adopted_or_cancelled(tmp_path, outcome):
    result = subprocess.run(
        [sys.executable, "-c", HARNESS, str(tmp_path), outcome],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "offline_completed": "completed",
        "offline_failed": "interrupted",
        "cancel_before_restart": "cancelled",
        "commit_window": "completed",
        "step_window": "interrupted",
    }.get(outcome, outcome)
    assert json.loads(result.stdout) == {"status": expected, "reaped": True}
