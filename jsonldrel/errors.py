"""诊断与拒绝：任何不支持/非法输入都以显式诊断拒绝，绝不静默忽略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Diagnostic:
    severity: str  # 'error' | 'note'
    code: str
    pointer: Optional[str]
    message: str


class Reject(Exception):
    """文件被拒绝（携带结构化诊断）。"""

    def __init__(self, code: str, message: str, pointer: Optional[str] = None):
        super().__init__(f"[{code}] {message} (at {pointer or '/'})")
        self.code = code
        self.message = message
        self.pointer = pointer

    def to_diagnostic(self) -> Diagnostic:
        return Diagnostic("error", self.code, self.pointer, self.message)
