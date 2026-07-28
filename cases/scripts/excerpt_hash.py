#!/usr/bin/env python3
"""计算 excerpt_sha256。

用法：
    python scripts/excerpt_hash.py "要计算的原文引用文本"
    echo "原文文本" | python scripts/excerpt_hash.py --
"""

import hashlib
import sys
import unicodedata
import re


def excerpt_sha256(text: str) -> str:
    """按 DATASET-CONTRIBUTION-GUIDE.md 规范计算 excerpt SHA-256：
    1. Unicode NFKC 归一化
    2. 折叠所有连续空白字符（含换行、Tab 等）为单个空格
    3. UTF-8 字节的 SHA-256
    """
    normalized = unicodedata.normalize("NFKC", text)
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--":
            text = sys.stdin.read()
        else:
            text = sys.argv[1]
    else:
        text = sys.stdin.read()

    h = excerpt_sha256(text)
    print(f"excerpt_sha256: {h}")
    return h


if __name__ == "__main__":
    main()
