#!/usr/bin/env python3
"""
qml_settings_help.py - tooltip texts for the settings web page from the gui-v1 QML of Venus.

Every QML item of the classic GUI that binds a com.victronenergy.settings path carries the label shown in the GUI
(description: qsTr("...")), for option lists also the text of each value (MbOption), and lives on a page with a title.
This walks all *.qml with a brace parser, resolves the path properties of the page (cgwacsPath, settingsPrefix, ...)
and writes {path: {label, page, options: {value: text}, unit}} as JSON. Heuristic: paths the GUI builds at run time
are missed; NOTES in index.php adds hand-written texts for the important ones.

Usage (on the Venus host, the rootfs is root-only):
  sudo python3 qml_settings_help.py /var/lib/machines/venus/opt/victronenergy/gui/qml > settings_help.json
"""
import glob
import json
import os
import re
import sys

STR = r'"((?:[^"\\]|\\.)*)"'
RE_PROP = re.compile(r'property\s+string\s+(\w+)\s*:\s*(.+)$')
RE_DESC = re.compile(r'\b(?:description|text|name)\s*:\s*qsTr\(\s*' + STR)
RE_BIND = re.compile(r'\bbind\s*:\s*(.+)$')
RE_VALUE = re.compile(r'\bvalue\s*:\s*(-?[\d.]+)')
RE_UNIT = re.compile(r'\bunit\s*:\s*' + STR)
RE_TITLE = re.compile(r'\btitle\s*:\s*qsTr\(\s*' + STR)


def unq(s):
	return s.encode().decode('unicode_escape')


def resolve(expr, props):
	"""string expression of a bind or property -> text, None when not resolvable"""
	expr = expr.strip().rstrip(';')
	m = re.fullmatch(r'Utils\.path\((.+)\)', expr)
	if m:
		parts = [resolve(p, props) for p in split_args(m.group(1))]
		return None if None in parts else ''.join(parts)
	parts = [p.strip() for p in re.split(r'\s*\+\s*', expr)]
	out = []
	for p in parts:
		if re.fullmatch(STR, p):
			out.append(unq(p[1:-1]))
		elif p in props and props[p] is not None:
			out.append(props[p])
		else:
			return None
	return ''.join(out)


def split_args(s):
	args, depth, cur, q = [], 0, '', False
	for ch in s:
		if ch == '"':
			q = not q
		if not q and ch == '(':
			depth += 1
		if not q and ch == ')':
			depth -= 1
		if not q and depth == 0 and ch == ',':
			args.append(cur)
			cur = ''
		else:
			cur += ch
	args.append(cur)
	return args


def settings_path(full, expr=''):
	if not full:
		# prefix handed in by the caller (bindPrefix, settingsPrefix of a sub page): take the literal /Settings/ part
		lit = ''.join(unq(x) for x in re.findall(STR, expr))
		i = lit.find('/Settings/')
		return lit[i:] if i >= 0 and '/' in lit[i + 10:].strip('/') + '/' else None
	full = full.replace('dbus/', '', 1) if full.startswith('dbus/') else full
	if full.startswith('com.victronenergy.settings/Settings/'):
		return full[len('com.victronenergy.settings'):]
	return None


class Block:
	def __init__(self, head):
		self.head = head          # text before the '{' on the same line, e.g. "MbItemOptions" or "item"
		self.own = []             # text directly in this block
		self.children = []

	def text(self):
		return ''.join(self.own)


def tree(src):
	root, stack, q, line = Block(''), None, False, ''
	stack = [root]
	for ch in src:
		if ch == '"':
			q = not q
		if not q and ch == '{':
			b = Block(stack[-1].text().rsplit('\n', 1)[-1].strip())
			stack[-1].children.append(b)
			stack.append(b)
		elif not q and ch == '}' and len(stack) > 1:
			stack.pop()
		else:
			stack[-1].own.append(ch)
	return root


def first(regex, text):
	m = regex.search(text)
	return m.group(1) if m else None


def bind_of(text):
	m = re.search(r'\bbind\s*:\s*([^;\n}]+)', text)
	return m.group(1) if m else None


def parse_file(fn, help_):
	src = open(fn, encoding='utf-8', errors='replace').read()
	props = {}
	for line in src.splitlines():
		m = RE_PROP.search(line)
		if m:
			props[m.group(1)] = resolve(m.group(2), props)
	tm = RE_TITLE.search(src)
	page = unq(tm.group(1)) if tm else os.path.basename(fn)[:-4]

	def walk(b):
		own = b.text()
		label = first(RE_DESC, own)
		if label and not b.head.startswith('MbOption'):
			binds = [bind_of(own)] + [bind_of(c.text()) for c in b.children if c.head.startswith('item')]
			for expr in filter(None, binds):
				path = settings_path(resolve(expr, props), expr)
				if not path:
					continue
				e = help_.setdefault(path, {'label': unq(label), 'page': page})
				opts = [c for c in b.children if c.head.startswith('MbOption')]
				comp = b.head.split()[-1] if b.head else ''
				for v, d in COMPONENT_OPTIONS.get(comp, {}).items():
					e.setdefault('options', {}).setdefault(v, d)
				for c in b.children:      # possibleValues: [ MbOption {..}, .. ] sits in the own text of b
					opts += [g for g in c.children if g.head.startswith('MbOption')]
				for o in opts:
					d, v = first(RE_DESC, o.text()), first(RE_VALUE, o.text())
					if d and v:
						e.setdefault('options', {})[v] = unq(d)
				unit = first(RE_UNIT, own) or next((first(RE_UNIT, c.text()) for c in b.children
													if c.head.startswith('item') and first(RE_UNIT, c.text())), None)
				if unit:
					e['unit'] = unq(unit)
				break
		for c in b.children:
			walk(c)

	walk(tree(src))


COMPONENT_OPTIONS = {}    # QML component (file name) -> its built-in MbOption values, e.g. MbItemAlarmEnableLevel


def component_options(fn):
	opts = {}

	def walk(b):
		if b.head.startswith('MbOption'):
			d, v = first(RE_DESC, b.text()), first(RE_VALUE, b.text())
			if d and v:
				opts.setdefault(v, unq(d))
		for c in b.children:
			walk(c)
	walk(tree(open(fn, encoding='utf-8', errors='replace').read()))
	return opts


def main():
	help_ = {}
	files = sorted(glob.glob(os.path.join(sys.argv[1], '**', '*.qml'), recursive=True))
	for fn in files:
		name = os.path.basename(fn)[:-4]
		if name.startswith('Mb'):
			o = component_options(fn)
			if o:
				COMPONENT_OPTIONS[name] = o
	for fn in files:
		parse_file(fn, help_)
	json.dump(dict(sorted(help_.items())), sys.stdout, ensure_ascii=False, indent=1)
	print(f'{len(help_)} paths', file=sys.stderr)


if __name__ == '__main__':
	main()
