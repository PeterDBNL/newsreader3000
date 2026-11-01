"""Heuristics for turning Usenet subjects into files and releases.

Typical binary subject:
    [03/15] - "Some.Release.part02.rar" yEnc (12/137) 98765432
  file key    -> everything except the trailing (12/137) segment counter
  release key -> file key without file counter, filename and size
"""
import re

SEG_RE = re.compile(r"[\(\[]\s*(\d+)\s*/\s*(\d+)\s*[\)\]]")
QUOTED_RE = re.compile(r'"([^"]+)"')
FILE_COUNTER_RE = re.compile(r"[\[\(]\s*\d+\s*(?:/|of)\s*\d+\s*[\]\)]|\b\d+\s+of\s+\d+\b", re.I)
EXT_RE = re.compile(
    r"(\.vol\d+\+\d+\.par2|\.part\d+\.rar|\.r\d{2,3}|\.s\d{2}|\.rar|\.par2|\.7z(\.\d{3})?|\.zip|\.z\d{2}"
    r"|\.\d{3}|\.nfo|\.sfv|\.nzb|\.srr|\.jpg|\.png|\.txt)$", re.I)
FILENAME_RE = re.compile(r"(\S+\.[A-Za-z0-9]{2,4})(?=\s|$)")
YENC_RE = re.compile(r"\byenc\b", re.I)
TRAILING_SIZE_RE = re.compile(r"\s+\d{3,}\s*$")


def strip_ext(name):
    prev = None
    while prev != name:
        prev = name
        name = EXT_RE.sub("", name)
    return name


def parse_subject(subject):
    """Return (file_key, release_key, part, total, filename)."""
    subject = subject or ""
    matches = list(SEG_RE.finditer(subject))
    part, total = 1, 1
    file_key = subject
    if matches and (YENC_RE.search(subject) or QUOTED_RE.search(subject) or len(matches) > 1
                    or matches[-1].end() >= len(subject.rstrip()) - 12):
        m = matches[-1]
        part, total = int(m.group(1)), int(m.group(2))
        file_key = (subject[:m.start()] + subject[m.end():])
        file_key = TRAILING_SIZE_RE.sub("", file_key)
    file_key = " ".join(file_key.split())

    q = QUOTED_RE.search(file_key)
    filename = q.group(1) if q else ""
    rel = file_key
    if q:
        rel = file_key[:q.start()] + " " + strip_ext(filename) + " " + file_key[q.end():]
    else:
        fm = FILENAME_RE.search(file_key)
        if fm and EXT_RE.search(fm.group(1)):
            filename = fm.group(1)
            rel = file_key[:fm.start()] + strip_ext(filename) + file_key[fm.end():]
    rel = FILE_COUNTER_RE.sub(" ", rel)
    rel = YENC_RE.sub(" ", rel)
    rel = re.sub(r"\s+-\s+(?=-|$)|^\s*-\s+", " ", rel)
    rel = " ".join(rel.replace(" - - ", " - ").split()).strip(" -")
    return file_key, rel or file_key, part, total, filename


def is_binary_subject(subject):
    return bool(YENC_RE.search(subject or "")) or bool(SEG_RE.search(subject or ""))


FILE_COUNT_RE = re.compile(r"[\[\(]\s*(\d+)\s*(?:/|of)\s*(\d+)\s*[\]\)]|\b(\d+)\s+of\s+(\d+)\b", re.I)
RANDOM_RE = re.compile(r"^[A-Za-z0-9]{12,}$")
HEXISH_RE = re.compile(r"^[0-9a-fA-F-]{16,}$")


def file_counter(file_key):
    """The [3/45] style file counter of a post: (3, 45), or (0, 0) when there is none."""
    m = FILE_COUNT_RE.search(file_key or "")
    if not m:
        return 0, 0
    a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(a), int(b)


def is_obfuscated(release_key, filename):
    """True when the file name carries no usable release name (random strings or no name at all),
    so files of one post can't be matched by name."""
    base = strip_ext(filename) if filename else ""
    if base:
        if HEXISH_RE.match(base):
            return True
        if RANDOM_RE.match(base) and re.search(r"\d", base) and re.search(r"[A-Za-z]", base):
            return True
        return False
    words = re.sub(r"[\W_]+", " ", release_key or "").split()
    return len(" ".join(words)) < 4


OBF_PREFIX = "Obfuscated post"


def obfuscated_prefix(poster, nfiles):
    return f"{OBF_PREFIX} · {nfiles or '?'} files · {poster} · #"
