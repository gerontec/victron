#!/usr/bin/env python3
"""
compare.py: which controller gets the PCC closer to the target (default 0 W): soyo (the setpoints batmonitor really
sent, measured) or the PI prototype (simulated on the recorded data), from /data/batmonitor/pi_shadow.csv (+ .1).

soyo:  PCC as measured while its setpoints were in effect.
PI:    closed-loop simulation with the parameters of batmonitor.c on the recorded disturbance
           d(k) = pcc(k) + sp(k-1)          what house + PV would show at the PCC without the Multis
           pcc_pi(k) = d(k) - u(k-1)        plant gain 1, one cycle delay (setpoint k-1 acts in cycle k)
           u(k) = u(k-1) + KP*(e(k) - e(k-1)) + KI*dt*e(k), clamped to the cycle's pi_min/pi_max (gates of that row)
       e = y - target, y = pcc_pi + Bat1 term (Bat1 charge * 0.5; Bat1 discharge only while u > 0 or at night),
       |e| < DEADBAND -> 0. Same rules as the C code.
Limits: the Sofar holds its PCC at 0 with Bat1 while it can, so part of a change goes into Bat1 instead of the PCC
(gain < 1) and Bat1 is taken as recorded; charger/AC-in limits and ramps of the Multis are ignored. The one-step
column (pcc(k+1) - (pi(k) - sp(k)), the shadow proposal on top of the soyo setpoint) is printed for reference.

usage: compare.py [--target W] [--since 'YYYY-MM-DD HH:MM'] [--max-gap s] pi_shadow.csv [pi_shadow.csv.1 ...]
"""
import argparse
import csv
import math
from datetime import datetime

# batmonitor.c
PI_TARGET = 0.0
PI_DEADBAND = 75.0
PI_KP = 0.2
PI_KI = 0.06
BAT1_CHARGE_FACTOR = 0.5
NIGHT_PV_TH = 100.0


def load(paths):
	rows = []
	for p in paths:
		with open(p, newline='') as f:
			for r in csv.DictReader(f):
				try:
					rows.append({'ts': int(r['ts']), 'pcc': float(r['pcc_w']), 'bat1': float(r['bat1_w']),
								 'pv': float(r['pv_w']), 'umin': float(r['pi_min_w']), 'umax': float(r['pi_max_w']),
								 'sp': sum(int(r[k]) for k in ('sp_l1', 'sp_l2', 'sp_l3')),
								 'pi': sum(int(r[k]) for k in ('pi_l1', 'pi_l2', 'pi_l3')), 'armed': int(r['armed'])})
				except (KeyError, ValueError):
					continue
	rows.sort(key=lambda r: r['ts'])
	return rows


def stats(dev, dt):
	"""dev: PCC - target (W) per cycle, dt: seconds each value stands for"""
	n = len(dev)
	return {'n': n, 'mae': sum(abs(d) for d in dev) / n, 'rms': math.sqrt(sum(d * d for d in dev) / n),
			'import_wh': sum(-d * t for d, t in zip(dev, dt) if d < 0) / 3600,
			'export_wh': sum(d * t for d, t in zip(dev, dt) if d > 0) / 3600}


def mode(total):
	return 'charge' if total > 0 else 'discharge' if total < 0 else 'idle'


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument('csv', nargs='+')
	ap.add_argument('--target', type=float, default=PI_TARGET, help='PCC target in W (default 0, as the PI)')
	ap.add_argument('--since', help="only rows from this local time on, 'YYYY-MM-DD HH:MM'")
	ap.add_argument('--max-gap', type=float, default=10.0, help='max seconds between two rows, else the simulation restarts')
	a = ap.parse_args()
	rows = load(a.csv)
	if a.since:
		t0 = datetime.strptime(a.since, '%Y-%m-%d %H:%M').timestamp()
		rows = [r for r in rows if r['ts'] >= t0]
	rows = [r for r in rows if not r['armed']]   # armed rows: the PI was in the loop, nothing to compare
	dev = {'soyo': [], 'pi': [], 'onestep': []}
	dts, closer, by_mode, energy = [], {'pi': 0, 'soyo': 0, 'same': 0}, {}, {'soyo': 0.0, 'pi': 0.0}
	u = e_prev = None
	for prev, r, nxt in zip(rows, rows[1:], rows[2:] + [None]):
		dt = r['ts'] - prev['ts']
		if dt <= 0 or dt > a.max_gap:
			u = e_prev = None                       # gap: restart from the soyo setpoint
			continue
		if u is None:
			u, e_prev = float(prev['sp']), 0.0
		d = r['pcc'] + prev['sp']
		pcc_pi = d - u
		night = r['pv'] < NIGHT_PV_TH
		b1 = r['bat1'] * BAT1_CHARGE_FACTOR if r['bat1'] > 0 else (r['bat1'] if (u > 0 or night) else 0.0)
		e = pcc_pi + b1 - a.target
		if abs(e) < PI_DEADBAND:
			e = 0.0
		u_pi_prev = u
		u = min(r['umax'], max(r['umin'], u + PI_KP * (e - e_prev) + PI_KI * dt * e))
		e_prev = e
		dev['soyo'].append(r['pcc'] - a.target)
		dev['pi'].append(pcc_pi - a.target)
		if nxt is not None and 0 < nxt['ts'] - r['ts'] <= a.max_gap:
			dev['onestep'].append(nxt['pcc'] - (r['pi'] - r['sp']) - a.target)
		dts.append(dt)
		energy['soyo'] += abs(prev['sp']) * dt / 3600
		energy['pi'] += abs(u_pi_prev) * dt / 3600
		ds, dp = abs(r['pcc'] - a.target), abs(pcc_pi - a.target)
		closer['same' if abs(ds - dp) < 1 else 'pi' if dp < ds else 'soyo'] += 1
		m = by_mode.setdefault((mode(prev['sp']), mode(u_pi_prev)), {'n': 0, 'ds': 0.0, 'dp': 0.0})
		m['n'] += 1
		m['ds'] += ds
		m['dp'] += dp
	if not dts:
		print('no comparable rows')
		return
	print('%d cycles, %s .. %s (%.1f h), target %.0f W' % (len(dts),
		datetime.fromtimestamp(rows[0]['ts']).strftime('%Y-%m-%d %H:%M'),
		datetime.fromtimestamp(rows[-1]['ts']).strftime('%Y-%m-%d %H:%M'), (rows[-1]['ts'] - rows[0]['ts']) / 3600, a.target))
	print('\n  %-22s %8s %8s %11s %11s %14s' % ('PCC - target', 'MAE W', 'RMS W', 'import Wh', 'export Wh', 'Multis |P| Wh'))
	for who, title in (('soyo', 'soyo (measured)'), ('pi', 'PI (simulated)')):
		s = stats(dev[who], dts)
		print('  %-22s %8.0f %8.0f %11.1f %11.1f %14.1f' % (title, s['mae'], s['rms'], s['import_wh'], s['export_wh'], energy[who]))
	if dev['onestep']:
		s = stats(dev['onestep'], dts[:len(dev['onestep'])])
		print('  %-22s %8.0f %8.0f %11.1f %11.1f' % ('PI one-step (shadow)', s['mae'], s['rms'], s['import_wh'], s['export_wh']))
	n = sum(closer.values())
	print('\ncloser to the target per cycle: PI %d (%.0f %%), soyo %d (%.0f %%), same %d (%.0f %%)' % (
		closer['pi'], 100 * closer['pi'] / n, closer['soyo'], 100 * closer['soyo'] / n, closer['same'], 100 * closer['same'] / n))
	print('\nby mode (soyo / PI): cycles, mean |PCC - target| soyo / PI')
	for (ms, mp), m in sorted(by_mode.items(), key=lambda x: -x[1]['n']):
		print('  %-9s / %-9s %6d  %7.0f W / %7.0f W' % (ms, mp, m['n'], m['ds'] / m['n'], m['dp'] / m['n']))


if __name__ == '__main__':
	main()
