"""Find blobs a malware or archive scanner may refuse, in every ref of a git repository.

Usage: blob_scan.py <repo> [--refs <pattern> ...]

Reads each blob reachable from any ref (or only the refs named), classifies it by its
leading bytes and prints what a scanner may trip on: archives and compressed data,
encrypted or password-protected archives, executables, key and certificate files,
private keys in text, git pack files and opaque high-entropy data. Known media (PNG,
JPEG, GIF, WebP, ICO, fonts, SVG) and text pass. Exits 1 when anything is found, 0 when
the repository is clean, 2 on a usage error.
"""
import collections
import math
import re
import subprocess
import sys

FLAG_MAGIC = [
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"), (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x04\x22\x4d\x18", "lz4"), (b"MSCF", "cab"), (b"Cr24", "crx"), (b"!<arch>", "ar/deb"),
    (b"\xed\xab\xee\xdb", "rpm"), (b"Salted__", "openssl-encrypted"),
    (b"age-encryption.org", "age-encrypted"), (b"-----BEGIN PGP MESSAGE", "pgp-encrypted"),
    (b"MZ", "windows executable"), (b"\x7fELF", "elf executable"),
    (b"\xcf\xfa\xed\xfe", "mach-o executable"), (b"\xfe\xed\xfa\xcf", "mach-o executable"),
    (b"\xca\xfe\xba\xbe", "mach-o/java class"), (b"PACK", "git pack"), (b"\xfftOc", "git pack index"),
    (b"RIDX", "git reverse index"), (b"\x30\x82", "der (certificate or key)"),
]
NOTE_MAGIC = [(b"\x00asm", "wasm"), (b"%PDF", "pdf"), (b"SQLite format 3", "sqlite")]
MEDIA = [b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"RIFF", b"\x00\x00\x01\x00", b"wOFF", b"wOF2",
         b"\x00\x01\x00\x00", b"OTTO"]
KEY_EXT = re.compile(r"\.(pfx|p12|jks|keystore|der|key)$", re.I)
# A whole PEM block with a body, not a label quoted in code or docs.
PRIVATE_KEY = re.compile(
    rb"-----BEGIN ([A-Z0-9-]+ )?PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/=]{16,}\r?\n){2,}"
    rb"[A-Za-z0-9+/=]*\r?\n?-----END ([A-Z0-9-]+ )?PRIVATE KEY-----")


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, check=True).stdout


def entropy(b):
    c = collections.Counter(b)
    return -sum(v / len(b) * math.log2(v / len(b)) for v in c.values()) if b else 0.0


def classify(body, names):
    head = body[:600]
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "flag", "tar"
    for magic, kind in FLAG_MAGIC:
        if head.startswith(magic):
            return "flag", kind
    if any(KEY_EXT.search(n) for n in names):
        return "flag", "key or keystore file"
    for magic, kind in NOTE_MAGIC:
        if head.startswith(magic):
            return "note", kind
    # ISO base media (HEIC, AVIF, MP4, MOV) names its brand after a 4-byte box size.
    if any(head.startswith(m) for m in MEDIA) or head[4:8] == b"ftyp":
        return None, None
    try:
        body.decode("utf-8")
        if PRIVATE_KEY.search(body):
            return "flag", "private key in text"
        return None, None
    except UnicodeDecodeError:
        pass
    e = entropy(body[:65536])
    if e > 7.5:
        return "flag", "opaque high-entropy data"
    return "note", "unrecognised binary"


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    repo, refs = argv[0], argv[1:]
    if refs and refs[0] == "--refs":
        refs = refs[1:]
    names = collections.defaultdict(set)
    listing = git(repo, "rev-list", "--objects", *(refs or ["--all"]))
    for line in listing.decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path:
            names[sha].add(path)
    shas = list(names)
    found = collections.defaultdict(list)
    if shas:
        types = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                    data=("\n".join(shas) + "\n").encode()).decode().split("\n")
        blobs = [l.split()[0] for l in types if l.endswith(" blob")]
        out = git(repo, "cat-file", "--batch", data=("\n".join(blobs) + "\n").encode())
        i = 0
        while i < len(out):
            nl = out.index(b"\n", i)
            sha, _, size = out[i:nl].decode().split()
            body = out[nl + 1: nl + 1 + int(size)]
            i = nl + 2 + int(size)
            level, kind = classify(body, names[sha])
            if level:
                found[(level, kind)].append((sha, len(body), sorted(names[sha])[0]))
    flagged = 0
    for (level, kind) in sorted(found):
        items = found[(level, kind)]
        flagged += len(items) if level == "flag" else 0
        print(f"{level.upper()} {kind}: {len(items)}")
        for sha, size, path in items[:25]:
            print(f"  {sha[:10]} {size:>10}  {path}")
        if len(items) > 25:
            print(f"  ... {len(items) - 25} more")
    print(f"{'FAIL' if flagged else 'PASS'}: {flagged} flagged, {len(names)} paths scanned")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
