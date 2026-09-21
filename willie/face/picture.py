"""Pictures on the face: a Wikipedia photo, decoded without an imaging library.

"Hoe ziet een banaan eruit?" -> `find("banaan")` searches Wikipedia (Dutch first, then
English) for the best-matching article with a JPEG lead image at screen size. `djpeg`
(apt: libjpeg-turbo-progs) turns it into raw RGB in a short-lived child process, so no
Pillow in the voice process (D21) and nothing stays resident. `Picture.fit()` then
scales it into the card on the face once, in the caller's thread, not in the render loop.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Wikimedia asks every client to identify itself.
USER_AGENT = "WILL-E/0.1 (home robot; https://github.com/woetner/willie)"
WIKIS = ("nl", "en")
THUMB = 480            # longest side requested from Wikimedia = the panel's width
MAX_BYTES = 2_000_000  # a thumbnail at this size is ~50 kB; anything huge is refused


@dataclass
class Picture:
    title: str
    width: int
    height: int
    rgb: bytes          # width * height * 3, row-major
    source: str = ""    # article URL, for the log / the model's answer

    def fit(self, box_w: int, box_h: int) -> "Picture":
        """Nearest-neighbour scale to fit inside the box, keeping the aspect ratio."""
        scale = min(box_w / self.width, box_h / self.height)
        w, h = max(1, int(self.width * scale)), max(1, int(self.height * scale))
        xs = [min(self.width - 1, int(x / scale)) * 3 for x in range(w)]
        out = bytearray(w * h * 3)
        for y in range(h):
            row = int(y / scale) * self.width * 3
            src = self.rgb[row:row + self.width * 3]
            o = y * w * 3
            for x, sx in enumerate(xs):
                out[o + x * 3:o + x * 3 + 3] = src[sx:sx + 3]
        return Picture(self.title, w, h, bytes(out), self.source)


def _get(url: str, timeout: float = 8.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("image too large")
    return data


def search(subject: str, wiki: str) -> list[tuple[str, str]]:
    """(title, JPEG thumbnail URL) of the best-matching article, if its lead image is a JPEG.

    Only the top hit counts: falling through to lower hits gave confident nonsense on the
    bench ("stappenmotor" -> an article about image resolution)."""
    query = urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": subject, "gsrlimit": 5,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": THUMB,
        "format": "json", "formatversion": 2,
    })
    data = json.loads(_get(f"https://{wiki}.wikipedia.org/w/api.php?{query}"))
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))[:1]
    found = []
    for page in pages:
        url = (page.get("thumbnail") or {}).get("source", "")
        # PNG/SVG thumbnails are diagrams and logos more often than photos, and djpeg
        # only reads JPEG: skip them rather than carry a second decoder.
        if url and urllib.parse.urlparse(url).path.lower().endswith((".jpg", ".jpeg")):
            found.append((page["title"], url))
    return found


def decode_jpeg(data: bytes) -> tuple[int, int, bytes]:
    if not shutil.which("djpeg"):
        raise RuntimeError("djpeg missing: sudo apt install libjpeg-turbo-progs")
    result = subprocess.run(["djpeg", "-pnm"], input=data, capture_output=True, timeout=10, check=False)
    if result.returncode or not result.stdout.startswith(b"P6"):
        raise RuntimeError(f"djpeg failed: {result.stderr.decode(errors='replace')[:120]}")
    return parse_ppm(result.stdout)


def parse_ppm(ppm: bytes) -> tuple[int, int, bytes]:
    """Binary P6 with maxval 255 (what djpeg writes); comments allowed in the header."""
    fields, pos = [], 2
    while len(fields) < 3:
        while ppm[pos:pos + 1].isspace():
            pos += 1
        if ppm[pos:pos + 1] == b"#":
            pos = ppm.index(b"\n", pos) + 1
            continue
        end = pos
        while not ppm[end:end + 1].isspace():
            end += 1
        fields.append(int(ppm[pos:end]))
        pos = end
    width, height, maxval = fields
    if maxval != 255:
        raise RuntimeError("only 8-bit PPM is supported")
    pos += 1                                   # the single whitespace after maxval
    rgb = ppm[pos:pos + width * height * 3]
    if len(rgb) != width * height * 3:
        raise RuntimeError("truncated image")
    return width, height, rgb


def find(subject: str, subject_en: str = "") -> Picture:
    """The lead photo of the best-matching article: Dutch Wikipedia for `subject`, then
    English Wikipedia for `subject_en` (the model passes the English name) or `subject`."""
    subject = subject.strip()
    if not subject:
        raise RuntimeError("no subject")
    for wiki in WIKIS:
        term = (subject_en.strip() or subject) if wiki == "en" else subject
        for title, url in search(term, wiki):
            try:
                width, height, rgb = decode_jpeg(_get(url))
            except RuntimeError:
                continue
            page = f"https://{wiki}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            return Picture(title, width, height, rgb, page)
    raise RuntimeError(f"no photo found for {subject!r}")
