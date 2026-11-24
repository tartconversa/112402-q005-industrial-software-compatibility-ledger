"""附件与正文的校验和工具。

校验信息必须随原始快照一起落库：版本号相同不代表构建相同，
附件哈希是判断两次下载是否等价的唯一硬证据。
"""
from __future__ import annotations

import hashlib

ALGORITHMS = ("sha256", "sha512", "sha1", "md5")


def digest(data: bytes, algorithm: str = "sha256") -> str:
    """计算字节串的十六进制摘要。"""
    normalized = algorithm.lower().replace("-", "")
    if normalized not in {"sha256", "sha512", "sha1", "md5"}:
        raise ValueError(f"不支持的校验算法: {algorithm}")
    return hashlib.new(normalized, data).hexdigest()


def sha256_text(text: str) -> str:
    """文本（UTF-8）的 sha256，用于给公告正文版本定址。"""
    return digest(text.encode("utf-8"), "sha256")
