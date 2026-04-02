#!/usr/bin/env python3
import csv
import os
import sys
from typing import List, Dict, Optional

"""
Aggregate per-collection CSV files into a single artwork_data.csv.
- Scans immediate subdirectories of <collections_root>
- For each directory, looks for a single CSV file (top-level *.csv)
- Reads rows and ensures required fields exist:
    - artwork_file (string): file basename used by uploader
    - artwork_dir (string): directory name (subfolder under collections_root)
  Other columns are preserved as-is.
- Writes a unified CSV with headers = union of all encountered columns plus artwork_file and artwork_dir

Usage:
  python aggregate_csv.py <collections_root> <output_csv>
"""

REQUIRED_HEADERS = ["artwork_file", "artwork_dir"]

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.tif'}


def dir_has_images(path: str) -> bool:
    """Return True if path contains at least one image file directly."""
    try:
        return any(
            os.path.isfile(os.path.join(path, f))
            and os.path.splitext(f)[1].lower() in IMAGE_EXT
            for f in os.listdir(path)
        )
    except Exception:
        return False


def find_first_csv(path: str) -> Optional[str]:
    try:
        for entry in os.listdir(path):
            if entry.lower().endswith('.csv') and os.path.isfile(os.path.join(path, entry)):
                return os.path.join(path, entry)
    except Exception:
        return None
    return None


def load_rows(csv_path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        with open(csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalize keys to exact as provided; strip BOM from first header if present
                norm_row: Dict[str, str] = {}
                for k, v in row.items():
                    if k is None:
                        continue
                    kk = k.replace('\ufeff', '')
                    norm_row[kk] = v if v is not None else ''
                rows.append(norm_row)
    except Exception as e:
        print(f"aggregate_csv.py: Warning: failed to read CSV {csv_path}: {e}", file=sys.stderr)
    return rows


def ensure_artwork_file(row: Dict[str, str]) -> Optional[str]:
    # Try common field names to locate filename
    for key in ("artwork_file", "file", "filename", "image", "image_file"):
        v = row.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: aggregate_csv.py <collections_root> <output_csv>")
        sys.exit(2)
    root = sys.argv[1]
    out_csv = sys.argv[2]

    all_rows: List[Dict[str, str]] = []
    header_set = set(REQUIRED_HEADERS)

    if not os.path.isdir(root):
        print(f"aggregate_csv.py: Collections root not found: {root}", file=sys.stderr)
        # Still write an empty CSV with required headers
        with open(out_csv, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=REQUIRED_HEADERS)
            writer.writeheader()
        sys.exit(0)

    subdirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    subdirs.sort()

    def _process_collection(dir_path: str, artwork_dir: str):
        """Load rows from a collection directory and append to all_rows."""
        csv_path = find_first_csv(dir_path)
        if not csv_path:
            return
        rows = load_rows(csv_path)
        for row in rows:
            file_name = ensure_artwork_file(row)
            if not file_name:
                continue
            merged = dict(row)
            merged["artwork_file"] = file_name
            merged["artwork_dir"] = artwork_dir
            for k in merged.keys():
                header_set.add(k)
            all_rows.append(merged)

    for d in subdirs:
        dir_path = os.path.join(root, d)
        if dir_has_images(dir_path):
            # Flat collection: images live directly in this directory
            _process_collection(dir_path, d)
        else:
            # Possibly a multi-collection repo: look one level deeper
            try:
                inner = sorted([s for s in os.listdir(dir_path) if os.path.isdir(os.path.join(dir_path, s))])
            except Exception:
                inner = []
            for s in inner:
                sub_path = os.path.join(dir_path, s)
                if dir_has_images(sub_path):
                    _process_collection(sub_path, os.path.join(d, s))

    # Ensure required headers exist and stable order: required first, then others alpha-sorted
    other_headers = sorted([h for h in header_set if h not in REQUIRED_HEADERS])
    headers = REQUIRED_HEADERS + other_headers

    os.makedirs(os.path.dirname(out_csv) or '.', exist_ok=True)
    with open(out_csv, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in all_rows:
            # Make sure all headers exist
            out_row = {h: (row.get(h) or '') for h in headers}
            writer.writerow(out_row)

    print(f"aggregate_csv.py: Wrote {len(all_rows)} rows to {out_csv} with {len(headers)} columns")


if __name__ == '__main__':
    main()
