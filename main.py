"""Offline pooled Weibull forecasts; Python standard library only."""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, NormalDist


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def logit(rate):
    p = clip(rate / 100, 0.0001, 0.9999)
    return math.log(p / (1 - p))


def percent(x):
    return 100 / (1 + math.exp(-clip(x, -40, 40)))


def stamp(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def kind(t):
    text = (t.get('name', '') + ' ' + t.get('description', '')).lower()
    if t.get('tier') == 'platinum':
        return 'platinum'
    if any(s in text for s in ('chapter', 'final mission', 'complete the story')):
        return 'story'
    if any(s in text for s in ('collect', '100%', 'all ', 'every ', 'million')):
        return 'grind'
    return t.get('tier', 'bronze')


def curve(c, tau, k, t):
    return c * (-math.expm1(-min(700, (max(t, 0.001) / tau) ** k)))


def series(case):
    release = stamp(case['game']['release_date'][:10] + 'T00:00:00Z')
    history = sorted(case['history'], key=lambda h: stamp(h['at']))
    # Retain one snapshot per refresh, timestamped at first observation.
    fresh = []
    for h in history:
        signature = (h.get('players_tracked'), tuple(h['rates']))
        if not fresh or signature != fresh[-1][0]:
            fresh.append((signature, h))
    # Early press/reviewer cohorts can be replaced by the launch cohort.
    start = 0
    for j in range(1, len(fresh)):
        a, b = fresh[j - 1][1], fresh[j][1]
        pairs = [(x, y) for x, y in zip(a['rates'], b['rates'])
                 if isinstance(x, (int, float)) and isinstance(y, (int, float)) and x > 0]
        if (b.get('players_tracked', 0) > 2 * max(1, a.get('players_tracked', 0))
                and pairs and median([y / x for x, y in pairs]) < 0.65):
            start = j
    fresh = fresh[start:]
    result = []
    for i, trophy in enumerate(case['trophies']):
        obs = []
        for _, h in fresh:
            r = h['rates'][i] if i < len(h['rates']) else None
            if isinstance(r, (int, float)) and math.isfinite(r):
                obs.append((max(0.03, (stamp(h['at']) - release) / 86400),
                            clip(r, 0, 100), max(1, h.get('players_tracked') or 1)))
        # Bound both runtime and the information attributed to refresh frequency.
        if len(obs) > 24:
            obs = [obs[round(j * (len(obs) - 1) / 23)] for j in range(24)]
        result.append(obs)
    return result, max(0.03, (stamp(case['target']) - release) / 86400)


def candidates(obs, target, label):
    t, r, players = obs[-1] if obs else (1, 0.01, 1)
    r = max(0.01, r)
    if len(obs) >= 2:
        a = obs[max(0, len(obs) // 3 - 1)]
        slope = (logit(r) - logit(a[1])) / max(0.05, math.log(t / a[0]))
    else:
        slope = 0.5
    # Weak priors, not claims about this game's eventual completion rates.
    k0 = 1.35 if label == 'story' else 1.05
    f0 = clip(1 - max(0, slope) / (k0 + 0.5), 0.15, 0.95)
    if label == 'story':
        f0 = max(0.30, f0)
    tau0 = t / (-math.log(1 - f0)) ** (1 / k0)
    ys = [logit(y) for _, y, _ in obs]
    # Rounded rare rates and small tracked populations have larger logit noise.
    noise = max(0.12, min(1.0, 1 / math.sqrt(max(1, players * r / 100))))
    out = []
    fractions = [0.025, 0.05, 0.08, 0.12, 0.18, 0.25, 0.34, 0.44,
                 0.55, 0.66, 0.76, 0.84, 0.90, 0.94, 0.97, 0.99, 0.998]
    ks = [0.45, 0.65, 0.85, 1.05, 1.3, 1.6, 2.0, 2.5, 3.2]
    # The ceiling grid includes values near 100 without forcing them there.
    ceilings = sorted(set([min(99.999, r / f) for f in fractions] +
                          [max(r, c) for c in (2, 5, 10, 20, 40, 60, 80, 95, 99.999)]))
    for c in ceilings:
        if c <= r or c > 100:
            continue
        f = clip(r / c, 0.001, 0.99999)
        for k in ks:
            base_tau = t / (-math.log1p(-f)) ** (1 / k)
            for offset in (-0.18, -0.09, 0, 0.09, 0.18):
                tau = base_tau * math.exp(offset / k)
                residual = [(logit(curve(c, tau, k, x)) - y) for (x, _, _), y in zip(obs, ys)]
                # Huber loss resists denominator jumps and delayed syncs.
                loss = sum(2 * (math.sqrt(1 + (e / noise) ** 2) - 1) for e in residual)
                loss *= min(1, 10 / max(1, len(obs)))
                prior = ((math.log(k / k0) / 0.65) ** 2 +
                         (math.log(tau / tau0) / 1.3) ** 2) * 0.5
                pred = logit(curve(c, tau, k, target))
                out.append((loss * 0.5 + prior, pred, math.log(tau), math.log(k),
                            math.log(c), math.sqrt(sum(e * e for e in residual) / max(1, len(residual)))))
    if not out:
        out = [(0, logit(r), math.log(max(t, 0.03)), math.log(k0), math.log(min(r, 100)), 0)]
    return out, (logit(r), clip(slope, -3, 4), label, len(obs), t, noise)


def forecast(case):
    all_obs, target = series(case)
    fits = [candidates(obs, target, kind(t)) for obs, t in zip(all_obs, case['trophies'])]
    best = [min(grid, key=lambda a: a[0]) for grid, _ in fits]
    rows = []
    # Fixed normal quadrature: no random sampling or platform-dependent seeds.
    z = [NormalDist().inv_cdf((j + 0.5) / 41) for j in range(41)]
    for i, ((grid, info), obs, trophy) in enumerate(zip(fits, all_obs, case['trophies'])):
        level, slope, label, n, cut, noise = info
        if n < 3:
            # A repeated page has no identified time scale or ceiling.
            horizon = max(0, target - cut)
            spread = 0.85 + 0.25 * math.sqrt(horizon)
            if not obs:
                spread = 1.8
            q = [round(percent(level + NormalDist().inv_cdf(p) * spread), 6)
                 for p in (0.05, 0.25, 0.5, 0.75, 0.95)]
            rows.append({'id': trophy['id'], 'q': q})
            continue
        peers = []
        for j, (_, other) in enumerate(fits):
            if i == j or other[3] < 4:
                continue
            distance = ((level - other[0]) / 1.5) ** 2 + ((slope - other[1]) / 0.8) ** 2
            distance += 0 if label == other[2] else 1.5
            peers.append((math.exp(-distance / 2), best[j]))
        mass = sum(w for w, _ in peers)
        pooled = [sum(w * b[k] for w, b in peers) / max(1e-12, mass) for k in (2, 3, 4)]
        strength = min(0.7, mass / 8) if n >= 2 else min(0.3, mass / 8)
        scored = []
        for g in grid:
            penalty = strength * sum(((g[k] - p) / s) ** 2 for k, p, s in
                                     zip((2, 3, 4), pooled, (1.0, 0.6, 1.2))) / 2
            scored.append((g[0] + penalty, g))
        minimum = min(s for s, _ in scored)
        samples = []
        horizon = max(0, target - cut)
        stale = max(0, (stamp(case['at']) - stamp(case['game']['release_date'][:10] + 'T00:00:00Z')) / 86400 - cut)
        for score, g in scored:
            weight = math.exp(-min(740, score - minimum))
            if weight < 1e-7:
                continue
            growth = max(0, g[1] - level)
            spread = math.sqrt((min(0.8, g[5]) * 0.5) ** 2 +
                               (0.10 + 0.18 * math.sqrt(horizon)) ** 2 +
                               (0.15 * growth) ** 2 + (0.15 * min(2, stale)) ** 2)
            # Observed decline is not evidence of negative trophy acquisition:
            # mix a damped continuation of denominator dilution with growth.
            decline_mass = 0.55 if slope < -0.15 and n >= 3 else 0.0
            for normal in z:
                samples.append((g[1] + spread * normal, weight * (1 - decline_mass)))
                if decline_mass:
                    drift = slope * math.log1p(horizon / max(0.5, cut)) * 0.5
                    samples.append((level + drift + (spread + 0.2) * normal, weight * decline_mass))
        samples.sort()
        total_weight = sum(w for _, w in samples)
        q = []
        cumulative = 0
        cursor = 0
        for p in (0.05, 0.25, 0.5, 0.75, 0.95):
            while cursor < len(samples) - 1 and cumulative + samples[cursor][1] < total_weight * p:
                cumulative += samples[cursor][1]
                cursor += 1
            q.append(round(percent(samples[cursor][0]), 6))
        rows.append({'id': trophy['id'], 'q': q})
    return {'trophies': rows}


def main():
    source = Path(os.environ.get('MAKEFUN_INPUT_DIR', '/input'))
    destination = Path(os.environ.get('MAKEFUN_OUTPUT_DIR', '/output'))
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob('*.in')):
        result = forecast(json.loads(path.read_text(encoding='utf-8')))
        output = destination / (path.stem + '.out')
        if output.is_symlink():
            output.unlink()
        output.write_text(json.dumps(result, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
