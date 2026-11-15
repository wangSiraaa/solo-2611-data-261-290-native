"""MARC21 880 替代文字字段互链抽取工具。"""

from .extract import extract_file, extract_bytes
from .inspect import inspect_pair

__all__ = ["extract_bytes", "extract_file", "inspect_pair"]
