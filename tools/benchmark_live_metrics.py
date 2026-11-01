"""Compare read-only own-worker telemetry; never changes host configuration.

Only aggregate timing distributions are printed: no process names, paths, PIDs,
TIDs, network traffic or user data. Worker threads belong to this temporary
benchmark process and wait until the finally block releases and joins them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import psutil
from optimus.live_metrics import _read_system
from optimus.thread_metrics import read_own_thread_times


def distribution(values):
    ordered = sorted(values)
    return dict(median_ms=round(statistics.median(ordered), 4),
                p95_ms=round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 4),
                min_ms=round(ordered[0], 4), max_ms=round(ordered[-1], 4))


def compare(before, after, samples):
    for operation in (before, after):
        operation()
        operation()
    observed = {'before': [], 'after': []}
    cpu_ns = dict(before=0, after=0)
    for index in range(samples):
        # Alternation avoids always giving one implementation the first slot.
        pair = [('before', before), ('after', after)]
        for name, operation in (pair if index % 2 == 0 else list(reversed(pair))):
            cpu_start, started = time.process_time_ns(), time.perf_counter_ns()
            operation()
            observed[name].append((time.perf_counter_ns() - started) / 1_000_000)
            cpu_ns[name] += time.process_time_ns() - cpu_start
    result = {name: dict(distribution(values), cpu_ms_total=round(cpu_ns[name] / 1_000_000, 3))
              for name, values in observed.items()}
    old, new = statistics.median(observed['before']), statistics.median(observed['after'])
    result['median_sample_cost_reduction_percent'] = round((1 - new / old) * 100, 2) if old else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This comparison targets the Windows thread-query optimization.')
    if not 5 <= args.samples <= 200 or not 1 <= args.workers <= 12:
        parser.error('Use 5 to 200 samples and 1 to 12 own worker threads.')
    release = threading.Event()
    workers = [threading.Thread(target=release.wait, name='Telemetry benchmark worker', daemon=True)
               for _ in range(args.workers)]
    try:
        for worker in workers:
            worker.start()
        selected = tuple(worker.native_id for worker in workers)
        wanted = set(selected)
        observed = read_own_thread_times(selected)
        if observed['unavailable'] or set(observed['threads']) != wanted:
            raise RuntimeError('The own-worker native identity checks did not pass; no comparison is valid.')

        def legacy_threads():
            return {row.id: row.user_time + row.system_time
                    for row in psutil.Process().threads() if row.id in wanted}

        def native_threads():
            return read_own_thread_times(selected)

        def legacy_system():
            raw = _read_system(())
            raw['threads'] = legacy_threads()
            return raw

        report = dict(schema=1, read_only=True, platform='win32', workers=args.workers,
            samples_per_implementation=args.samples, process_time_resolution_seconds=time.get_clock_info('process_time').resolution,
            thread_query=compare(legacy_threads, native_threads, args.samples),
            full_sample=compare(legacy_system, lambda: _read_system(selected), args.samples),
            note='Observed sampler overhead only. This is not a benchmark of whole-PC performance or installer speed; timing varies with system load and CPU timers have finite resolution.')
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
