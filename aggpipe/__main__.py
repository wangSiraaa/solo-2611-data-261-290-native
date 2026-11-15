"""支持 `python -m aggpipe ...` 调用。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
