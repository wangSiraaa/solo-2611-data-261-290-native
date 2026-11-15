"""jsonldrel：离线 JSON-LD 子集 → 关系事实表（nodes/facts/list_members/diagnostics）。

公开 API：
- import_document(path, db_path=None) -> ImportResult   # import 主流程
- Inspector(conn)                                       # inspect 查询
"""

from .api import ImportResult, import_document
from .inspect import Inspector

__all__ = ["ImportResult", "Inspector", "import_document"]
