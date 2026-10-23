"""Dependency-free PDF writer (Helvetica, A4, wrapped text, multiple pages). Pure Python so it runs on Workers Free.
Limits: built-in Helvetica covers Latin-1 only; any other character (e.g. Devanagari) becomes '?'. No images."""
import re

W, H, M, SIZE, LEAD = 595, 842, 54, 11, 15
MAXW = W - 2 * M


def _clean(s):
    s = str(s).replace("\r", "").replace("\t", "    ")
    s = re.sub(r"[\u2018\u2019]", "'", s)
    s = re.sub(r"[\u201c\u201d]", '"', s)
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2022", "-").replace("\u2026", "...")
    return s.encode("latin-1", "replace").decode("latin-1")


class UnsupportedText(ValueError):
    pass


def unsupported_chars(s):
    s = re.sub(r"[\u2018\u2019\u201c\u201d\u2013\u2014\u2022\u2026]", "", str(s))
    return sorted({c for c in s if ord(c) > 0xFF})


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(line, size):
    # Helvetica average glyph width about 0.5 em: conservative wrap
    per = max(10, int(MAXW / (size * 0.52)))
    out = []
    while len(line) > per:
        cut = line.rfind(" ", 0, per)
        cut = cut if cut > 0 else per
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    out.append(line)
    return out


def make_pdf(title, text, max_pages=20):
    bad = unsupported_chars(str(title) + str(text))
    if bad:
        raise UnsupportedText("unsupported characters: " + "".join(bad[:10]))
    lines = [(_clean(title), 16, True), ("", SIZE, False)] if title else []
    for raw in _clean(text).split("\n"):
        for l in _wrap(raw, SIZE):
            lines.append((l, SIZE, False))
    per_page = int((H - 2 * M) / LEAD)
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)][:max_pages] or [[("", SIZE, False)]]
    objs = []  # index = obj number - 1
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    # objects 1..4 reserved above (4 = bold font); page objects start at 5
    objs = objs[:2] + [objs[2], objs[3]]
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n_pages))
    objs[1] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>"
    for i, pg in enumerate(pages):
        ops = ["BT"]
        y = H - M
        for (l, size, bold) in pg:
            ops.append(f"/{'F2' if bold else 'F1'} {size} Tf 1 0 0 1 {M} {y} Tm ({_esc(l)}) Tj")
            y -= LEAD
        ops.append("ET")
        stream = "\n".join(ops)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n".encode() + o.encode("latin-1") + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)
