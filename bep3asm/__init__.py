"""BEP3 多文件分片装配工具（离线，仅标准库）。

公开两个入口：

* :func:`bep3asm.assemble.run_assemble` —— 校验并装配恢复目录；
* :func:`bep3asm.inspect.inspect_torrent` /
  :func:`bep3asm.inspect.inspect_package` —— 只读检查。
"""

from .errors import AssembleError

__all__ = ["AssembleError"]
