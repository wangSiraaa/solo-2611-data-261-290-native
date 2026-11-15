"""工具内部统一异常类型。"""

from __future__ import annotations


class AssembleError(Exception):
    """输入非法或装配前置条件不满足时抛出。

    所有校验都在输出目录被创建之前完成，因此抛出该异常时磁盘上不会
    留下任何半成品恢复目录。
    """
