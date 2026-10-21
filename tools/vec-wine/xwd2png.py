#!/usr/bin/env python3
"""xwd2png.py - convert an xwd screenshot (TrueColor ZPixmap, 24/32 bpp, as written by `xwd`) to PNG,
standard library only (no ImageMagick / PIL on the Pi).  usage: xwd2png.py in.xwd out.png [x y w h]"""
import struct
import sys
import zlib


def mask_shift(m):
	s = 0
	while m and not m & 1:
		m >>= 1
		s += 1
	return s


def convert(src, dst, crop=None):
	d = open(src, 'rb').read()
	h = struct.unpack('>25I', d[:100])
	hsize, width, height = h[0], h[4], h[5]
	byte_order, bpp, bpl = h[7], h[11], h[12]
	rm, gm, bm, ncolors = h[14], h[15], h[16], h[19]
	if bpp not in (24, 32):
		sys.exit(f'{src}: {bpp} bits per pixel not supported')
	pix = d[hsize + 12 * ncolors:]
	rs, gs, bs = mask_shift(rm), mask_shift(gm), mask_shift(bm)
	x0, y0, w, hh = crop or (0, 0, width, height)
	step, order = bpp // 8, 'little' if byte_order == 0 else 'big'
	rows = []
	for y in range(y0, y0 + hh):
		line = pix[y * bpl:(y + 1) * bpl]
		row = bytearray(b'\0')
		for x in range(x0, x0 + w):
			v = int.from_bytes(line[x * step:x * step + step], order)
			row += bytes(((v & rm) >> rs, (v & gm) >> gs, (v & bm) >> bs))
		rows.append(bytes(row))

	def chunk(tag, data):
		return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))

	png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, hh, 8, 2, 0, 0, 0))
		   + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))
	open(dst, 'wb').write(png)


if __name__ == '__main__':
	convert(sys.argv[1], sys.argv[2], tuple(map(int, sys.argv[3:7])) if len(sys.argv) >= 7 else None)
