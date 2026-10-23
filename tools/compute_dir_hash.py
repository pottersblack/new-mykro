#!/usr/bin/env python3
"""Compute a deterministic SHA256 hash of all files under a directory.
Usage: python tools/compute_dir_hash.py <dir>
Outputs JSON with files list and sha256 digest.
"""
import sys
import os
import hashlib
import json

def compute_dir_hash(root_dir: str) -> dict:
    entries = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root_dir).replace("\\", "/")
            try:
                with open(fp, "rb") as f:
                    content = f.read()
            except OSError:
                # If a file cannot be read (permission, removed during walk), skip its content
                content = b""
            file_hash = hashlib.sha256(content).hexdigest()
            entries.append((rel, file_hash))
    entries.sort()
    h = hashlib.sha256()
    for rel, fh in entries:
        h.update(rel.encode("utf-8"))
        h.update(b"\n")
        h.update(fh.encode("utf-8"))
        h.update(b"\n")
    return {"digest": h.hexdigest(), "files": [{"path": r, "sha256": f} for r, f in entries]}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: compute_dir_hash.py <dir>")
        sys.exit(2)
    root = sys.argv[1]
    out = compute_dir_hash(root)
    print(json.dumps(out, indent=2))
