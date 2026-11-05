"""Writes a tiny but valid PDF (text lines + optional link annotation) without extra libraries."""


def make_pdf(lines, link=None):
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")

    stream = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({esc(l)}) '" for l in lines) + " ET"
    annots = "/Annots [5 0 R]" if link else ""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        f"/Resources << /Font << /F1 6 0 R >> >> {annots} >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        f"<< /Type /Annot /Subtype /Link /Rect [50 50 150 70] /A << /S /URI /URI ({esc(link or '')}) >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")
