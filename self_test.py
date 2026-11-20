"""Contract, recovery, sparse data, refresh, and deterministic executable checks."""
import copy
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from main import curve, logit, series

ROOT = Path(__file__).resolve().parent
RELEASE = datetime(2026, 11, 19, tzinfo=timezone.utc)


def iso(days):
    return (RELEASE + timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def synthetic(seed, family='weibull'):
    rng = random.Random(seed)
    trophies, parameters = [], []
    for i in range(55):
        group = i % 5
        c = rng.uniform(55, 95) if group == 0 else rng.uniform(1, 45)
        tau = rng.uniform(0.25, 1.1) if group == 0 else rng.uniform(0.8, 5)
        k = rng.uniform(0.8, 2.0)
        parameters.append((c, tau, k))
        trophies.append({'id': i + 1, 'name': f'Trophy {i + 1}',
                         'description': 'Complete Chapter 1.' if group == 0 else 'Do something.',
                         'tier': 'bronze' if group else 'gold', 'points': 15, 'secret': False})
    def rate(p, t):
        c, tau, k = p
        if family == 'hill':
            return c * t ** k / (tau ** k + t ** k)
        return curve(c, tau, k, t)
    history = []
    for j in range(24):
        t = 0.12 + j * 1.88 / 23
        rates = [round(max(0, min(100, rate(p, t) * math.exp(rng.gauss(0, 0.025)))), 2)
                 for p in parameters]
        history.append({'slot': iso(t), 'at': iso(t), 'players_tracked': 15000 + j * 1000,
                        'trophies_earned': 0, 'rates': rates})
    case = {'schema': 'makefun/gta6-trophies-case/v1', 'id': 'synthetic', 'at': iso(2.01),
            'target': iso(7), 'horizon_minutes': 7186, 'levels': [.05, .25, .5, .75, .95],
            'game': {'release_date': '2026-11-19', 'name': 'Synthetic', 'platform': 'PS5'},
            'trophies': trophies, 'history': history, 'notes': 'Invented data only.'}
    return case, [rate(p, 7) for p in parameters]


def validate(raw, case):
    assert len(raw) <= 65536
    x = json.loads(raw)
    assert set(x) == {'trophies'}
    assert len(x['trophies']) == len(case['trophies'])
    assert sorted(t['id'] for t in x['trophies']) == sorted(t['id'] for t in case['trophies'])
    for t in x['trophies']:
        assert set(t) == {'id', 'q'} and len(t['q']) == 5
        assert all(type(v) in (float, int) and math.isfinite(v) and 0 <= v <= 100 for v in t['q'])
        assert sorted(t['q']) == t['q']
    return x


def run(case):
    with tempfile.TemporaryDirectory() as tmp:
        source, output = Path(tmp) / 'input', Path(tmp) / 'output'
        source.mkdir()
        (source / 'case.in').write_text(json.dumps(case))
        env = dict(os.environ, MAKEFUN_INPUT_DIR=str(source), MAKEFUN_OUTPUT_DIR=str(output))
        start = time.monotonic()
        subprocess.run([sys.executable, '-s', str(ROOT / 'main.py')], env=env, check=True, timeout=8)
        elapsed = time.monotonic() - start
        raw = (output / 'case.out').read_bytes()
        result = validate(raw, case)
        subprocess.run([sys.executable, '-s', str(ROOT / 'main.py')], env=env, check=True, timeout=8)
        assert (output / 'case.out').read_bytes() == raw
        return result, elapsed


def main():
    for family in ('weibull', 'hill'):
        case, truth = synthetic(823, family)
        result, elapsed = run(case)
        errors = [abs(r['q'][2] - y) for r, y in zip(result['trophies'], truth)]
        log_errors = [abs(logit(r['q'][2]) - logit(y)) for r, y in zip(result['trophies'], truth)]
        coverage = sum(r['q'][0] <= y <= r['q'][4] for r, y in zip(result['trophies'], truth)) / 55
        print(f'{family}: median absolute error={median(errors):.3f} pp; mean={sum(errors)/55:.3f} pp; '
              f'mean logit error={sum(log_errors)/55:.3f}; coverage={coverage:.3f}; runtime={elapsed:.3f}s')
        assert median(errors) < 4 and sum(log_errors) / 55 < 0.4
    case, _ = synthetic(24)
    for j, h in enumerate(case['history']):
        h['rates'][0] = None if j < 23 else 1.25  # single read
        h['rates'][1] = 0  # all zeros
        h['rates'][2] = None  # no observations
        h['rates'][3] = 100  # saturated boundary
        if j % 3 == 0:
            h['rates'][4] = None
    run(case)
    print('single read, zero, entirely missing, intermittent missing, and 100%: passed')
    repeated = copy.deepcopy(case)
    repeated['history'] = [copy.deepcopy(case['history'][-1]) for _ in range(100)]
    run(repeated)
    assert len(series(repeated)[0][4]) == 1
    print('unchanged snapshots: passed')
    reset = copy.deepcopy(case)
    for h in reset['history'][:12]:
        h['players_tracked'] = 100
        h['rates'] = [40] * 55
    for h in reset['history'][12:]:
        h['players_tracked'] = 1000
        h['rates'] = [5] * 55
    assert all(len(obs) == 1 and obs[0][1] == 5 for obs in series(reset)[0])
    print('launch cohort reset: passed')
    if len(sys.argv) > 1:
        for path in sorted(Path(sys.argv[1]).glob('*.in')):
            case = json.loads(path.read_text())
            result, elapsed = run(case)
            print(f'{path.name}: valid, {elapsed:.3f}s; output head: ' + json.dumps(result['trophies'][:3]))


if __name__ == '__main__':
    main()
