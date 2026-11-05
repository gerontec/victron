#!/usr/bin/env python3
"""rvms_diff.py - compare two mk2vsc backups (.rvms) of the MultiPlus-II by content, warn by mail on a change.

User 2026-11-05: "ich moechte eine warnmail wenn sich das file aendert". The files differ every night by the backup
time of each device, so only the content counts: firmware, the devices (serial, target id), the 192 setting words
(names from rvms2db.SETTINGS) and the assistant bytes (ESS constants decoded by rvms2db.ess_values).

usage: rvms_diff.py OLD.rvms NEW.rvms [--mail] [--test]
  exit 0 = same configuration, 1 = changed (with --mail: warning mail), 2 = a file could not be read
  --test puts TEST into the subject
Called by tools/vec-wine/nightly-config.sh with the newest file before the run and the new one.
"""
import os
import smtplib
import socket
import sys
from email.message import EmailMessage
from email.utils import formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rvms2db import SETTINGS, ess_values, parse_rvms  # noqa: E402

MAIL_TO = 'gh@heissa.de'
MAIL_FROM = 'alarm@heissa.de'
SMTP_HOSTS = ('heissa.de', '10.8.0.1')      # as tools/alarm_mail.py: the MX of heissa.de takes mail to gh@ (port 25)


def load(path):
	try:
		fw, _, devices = parse_rvms(path)
	except SystemExit as e:                  # parse_rvms exits on a bad file
		print(f'{path}: {e}', file=sys.stderr)
		sys.exit(2)
	return fw, {d['serial']: d for d in devices}


def differences(old_path, new_path):
	fw_o, old = load(old_path)
	fw_n, new = load(new_path)
	out = []
	if fw_o != fw_n:
		out.append(f'firmware {fw_o} -> {fw_n}')
	for s in sorted(set(old) - set(new)):
		out.append(f'device {s} is gone')
	for s in sorted(set(new) - set(old)):
		out.append(f'device {s} is new (target id 0x{new[s]["target_id"]:08x})')
	for s in sorted(set(old) & set(new)):
		o, n = old[s], new[s]
		if o['target_id'] != n['target_id']:
			out.append(f'{s}: target id 0x{o["target_id"]:08x} -> 0x{n["target_id"]:08x}')
		for sid, (a, b) in enumerate(zip(o['words'], n['words'])):
			if a != b:
				name = SETTINGS.get(sid, (f'setting_{sid}', '?'))[0]
				out.append(f'{s}: setting {sid} {name}: raw {a} -> {b}')
		if o['assistant'] != n['assistant']:
			eo = {r[0]: r for r in ess_values(o['assistant'])}
			en = {r[0]: r for r in ess_values(n['assistant'])}
			listed = False
			for sid in sorted(set(eo) | set(en)):
				a, b = eo.get(sid), en.get(sid)
				if (a and a[2]) != (b and b[2]):
					name = (a or b)[1]
					out.append(f'{s}: assistant {sid} {name}: {a[3] if a and a[3] is not None else (a[2] if a else "-")}'
							   f' -> {b[3] if b and b[3] is not None else (b[2] if b else "-")}')
					listed = True
			if not listed:
				out.append(f'{s}: assistant program changed ({len(o["assistant"])} -> {len(n["assistant"])} bytes, '
						   f'no decoded ESS constant differs)')
	return out


def send_mail(subject, body):
	msg = EmailMessage()
	msg['From'], msg['To'], msg['Subject'], msg['Date'] = MAIL_FROM, MAIL_TO, subject, formatdate(localtime=True)
	msg.set_content(body)
	for host in SMTP_HOSTS:
		try:
			with smtplib.SMTP(host, 25, timeout=20) as s:
				s.send_message(msg)
			print(f'mail via {host}: {subject}')
			return True
		except (OSError, smtplib.SMTPException) as e:
			print(f'mail via {host} failed: {e}', file=sys.stderr)
	return False


def host_ip():
	try:
		with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
			s.connect(('192.168.178.1', 9))
			return s.getsockname()[0]
	except OSError:
		return '?'


def main():
	args = [a for a in sys.argv[1:] if not a.startswith('--')]
	if len(args) != 2:
		sys.exit(__doc__)
	old_path, new_path = args
	diff = differences(old_path, new_path)
	if not diff:
		print(f'same configuration: {os.path.basename(old_path)} = {os.path.basename(new_path)}')
		return 0
	print(f'configuration changed: {os.path.basename(old_path)} -> {os.path.basename(new_path)}, {len(diff)} differences')
	for line in diff:
		print('  ' + line)
	if '--mail' in sys.argv:
		subject = ('TEST ' if '--test' in sys.argv else '') + \
			f'WARNING Lenggries: MultiPlus configuration changed ({len(diff)} differences)'
		body = (f'The nightly VE.Bus backup differs in content from the one before.\n\n'
				f'before: {old_path}\nnow:    {new_path}\n\n' + '\n'.join(diff) +
				f'\n\nNothing was written to the MultiPlus. The worst case state for a restore stays in '
				f'wagodb.vebus_config_backup (archive a new state with readers/rvms_archive.py).\n\n'
				f'host {socket.gethostname()} {host_ip()}, script {os.path.abspath(__file__)}, '
				f'cron ~/vec/nightly-config.sh\n')
		send_mail(subject, body)
	return 1


if __name__ == '__main__':
	sys.exit(main())
