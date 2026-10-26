#!/usr/bin/env python3
"""Minimal VRM API v2 client (https://vrmapi.victronenergy.com/v2), access token auth.

Token: env VRM_TOKEN or the file ~/.vrm_token (chmod 600, never into git). Create it in VRM:
Preferences -> Integrations -> Access tokens. Header: "x-authorization: Token <token>" (Bearer is deprecated since
2026-06-01). Rate limit: ~3 requests/s on average; a 429 is retried after its Retry-After header.

    vrm_api.py me                         # the user behind the token (idUser)
    vrm_api.py sites                      # installations of that user: idSite, name, portal id
    vrm_api.py site [PORTAL]              # idSite of a portal id (default: Lenggries Pi e45f01114b2c)
    vrm_api.py get PATH [key=value ...]   # any GET, PATH relative to /v2, {site} = idSite of the default portal
                                          # e.g. get installations/{site}/diagnostics
                                          #      get installations/{site}/stats type=kwh interval=days
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://vrmapi.victronenergy.com/v2'
TOKEN_FILE = os.path.expanduser('~/.vrm_token')
PORTAL = 'e45f01114b2c'      # Lenggries, Venus on Pi .123 (eth0 MAC)
TIMEOUT = 20


def token():
	t = os.environ.get('VRM_TOKEN')
	if not t:
		try:
			with open(TOKEN_FILE) as f:
				t = f.read().strip()
		except OSError:
			sys.exit(f"no token: set VRM_TOKEN or write it to {TOKEN_FILE} (chmod 600)")
	return t


def get(path, params=None, retries=3):
	url = f"{BASE}/{path.lstrip('/')}"
	if params:
		url += '?' + urllib.parse.urlencode(params)
	req = urllib.request.Request(url, headers={'x-authorization': f'Token {token()}', 'Accept': 'application/json'})
	for _ in range(retries):
		try:
			with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
				return json.load(r)
		except urllib.error.HTTPError as e:
			if e.code == 429:
				time.sleep(float(e.headers.get('Retry-After', 2)))
				continue
			body = e.read().decode(errors='replace')[:300]
			sys.exit(f"HTTP {e.code} {url}: {body}")
	sys.exit(f"still rate limited: {url}")


def me():
	return get('users/me')['user']


def sites():
	return get(f"users/{me()['id']}/installations")['records']


def site_id(portal=PORTAL):
	for s in sites():
		if s.get('identifier') == portal:
			return s['idSite']
	sys.exit(f"portal {portal} is not an installation of this VRM user")


def main():
	if len(sys.argv) < 2:
		sys.exit(__doc__)
	cmd, args = sys.argv[1], sys.argv[2:]
	if cmd == 'me':
		u = me()
		print(f"idUser {u['id']}  {u.get('name')}  {u.get('email')}")
	elif cmd == 'sites':
		for s in sites():
			print(f"{s['idSite']:8}  {s.get('identifier')}  {s.get('name')}")
	elif cmd == 'site':
		print(site_id(args[0] if args else PORTAL))
	elif cmd == 'get' and args:
		path = args[0]
		if '{site}' in path:
			path = path.replace('{site}', str(site_id()))
		params = dict(a.split('=', 1) for a in args[1:])
		print(json.dumps(get(path, params), indent=1, ensure_ascii=False))
	else:
		sys.exit(__doc__)


if __name__ == '__main__':
	main()
