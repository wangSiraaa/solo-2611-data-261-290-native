"""XLIFF 2.0 双语语料提取与读回核查（仅标准库，不做翻译/训练）。

公开入口：

* :func:`xliff_corpus.extractor.extract_file` —— 解析单个 .xlf，产出逐 segment
  的 token 清单、训练配对表、代码载荷表与诊断；
* :func:`xliff_corpus.inspect_readback.inspect_document` —— 重新读取提取结果，
  独立重建两个方向的 token 串与 pc 代码包含关系，做 read-back 核查。
"""

from .extractor import extract_file, ExtractionResult
from .inspect_readback import inspect_document

__all__ = ["extract_file", "ExtractionResult", "inspect_document"]
