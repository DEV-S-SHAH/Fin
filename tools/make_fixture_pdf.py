"""Generate multi-page prose PDFs with no third-party dependencies.

Used to create test fixtures for the GraphRAG pipeline. Deliberately writes raw
PDF syntax so the fixture does not depend on a PDF-authoring library.
"""

from __future__ import annotations

from pathlib import Path


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, width: int = 92) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if len(candidate) > width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def build_pdf(pages: list[str], font: str = "Helvetica", size: int = 11) -> bytes:
    """Render one PDF page per entry in *pages*."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    # Object numbers are 1-based and index into *objects* as obj_num - 1, so the
    # reserved slots must be allocated before anything that references them.
    catalog_obj = add(b"")  # 1: catalog
    pages_obj = add(b"")  # 2: page tree
    font_obj = add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode())  # 3

    page_objs: list[int] = []
    content_objs: list[int] = []
    for body in pages:
        lines = _wrap(body)
        parts = ["BT", f"/F1 {size} Tf", f"{size + 4} TL", "56 742 Td"]
        for index, line in enumerate(lines):
            escaped = _escape(line)
            if index == 0:
                parts.append(f"({escaped}) Tj")
            else:
                parts.append(f"T* ({escaped}) Tj")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1", "replace")

        content_objs.append(
            add(
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
        )
        page_objs.append(add(b""))

    page_count = len(page_objs)
    kids = " ".join(f"{num} 0 R" for num in page_objs)
    objects[catalog_obj - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>".encode()
    )

    for page_obj, content_obj in zip(page_objs, content_objs):
        objects[page_obj - 1] = (
            "<< /Type /Page "
            f"/Parent {pages_obj} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {content_obj} 0 R >>"
        ).encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


BIOLOGY = [
    """Marine biology study: the hydrothermal vent ecosystem. Researchers studying the
    Mid-Atlantic Ridge have documented a dense food web that depends entirely on
    chemosynthesis rather than sunlight. The central organism is Riftia pachyptila, a
    giant tubeworm that lacks a mouth and gut. Riftia pachyptila hosts symbiotic
    bacteria inside a specialized organ called the trophosome. The bacteria, which
    belong to the genus Candidatus Endoriftia, oxidize hydrogen sulfide to produce
    organic carbon. This process sustains the vent community.""",
    """The vent fields also contain the blind shrimp Rimicaris exoculata. Rimicaris
    exoculata grazes on bacterial mats near the chimney walls and harbors bacteria
    on its dorsal shell. Both Riftia pachyptila and Rimicaris exoculata rely on
    hydrogen sulfide produced by the vent fluid. The vent fluid reaches temperatures
    above 350 degrees Celsius. Nearby, the mussel Bathymodiolus thermophilus filters
    particulate organic matter from the cooler water surrounding the vents.""",
    """Vent chemistry shapes the whole community. The chimney structures are built from
    sulfide minerals, principally pyrite and chalcopyrite, deposited as hot fluid
    cools. Vesicles in the chimney release the hydrogen sulfide that the Endoriftia
    bacteria require. Because sunlight never penetrates this habitat, the food web
    of Riftia pachyptila, Rimicaris exoculata, and Bathymodiolus thermophilus is
    entirely chemotrophic. Scientists continue to survey new vent fields to expand
    the known range of Endoriftia and its bacterial mats.""",
]

COOKING = [
    """Sourdough bread method. A starter of flour and water ferments naturally using
    wild yeast and lactic acid bacteria. The baker combines starter with flour, water,
    and salt to form the dough. The baker shapes the dough, then proves the dough
    overnight in a refrigerator. Slow fermentation develops flavour in the dough
    crust. Steam during the first minutes of baking keeps the crust flexible so the
    oven spring can open the crumb.""",
    """Risotto technique. Arborio rice is toasted in butter before broth is added. The
    cook ladles hot broth into the rice gradually, stirring often so the rice releases
    starch. The cook finishes the risotto with butter and parmesan off the heat.
    Resting the risotto briefly before serving improves its texture. Stock kept at a
    gentle simmer prevents the rice from cooking unevenly.""",
]


STANDARD = [
    """Technical standard for interoperable telemetry, revision 4. Section 2.1 \
defines the Frame Checkpoint. Every Frame carries a Sequence Number and a \
Frame Type. A Collector validates the Frame Checkpoint before accepting a Frame. \
The Frame Type determines which Payload Schema applies. Rejected Frames are \
counted by the Rejection Counter.""",
    """Section 3.2 specifies transport. The Telemetry Gateway forwards Frames over \
the Message Bus. The Message Bus guarantees at-least-once delivery. Duplicate \
Frames are discarded by the Deduplication Window. The Retention Policy governs \
how long the Archive keeps a Frame. Operators tune the Deduplication Window and \
the Retention Policy independently.""",
    """Section 5 addresses conformance. A Conformance Suite exercises the Collector \
against the Reference Vectors. Any Device failing the Reference Vectors fails \
conformance. The Audit Log records every rejection emitted by the Frame \
Checkpoint. Certification bodies publish the Audit Log annually.""",
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "samples"
    out_dir.mkdir(exist_ok=True)
    targets = {
        "marine_biology.pdf": BIOLOGY,
        "cooking_methods.pdf": COOKING,
        "telemetry_standard.pdf": STANDARD,
    }
    for name, pages in targets.items():
        path = out_dir / name
        path.write_bytes(build_pdf(pages))
        print(f"wrote {path} ({path.stat().st_size} bytes, {len(pages)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
