"""One-shot: run the trading desk's own market-data refresh and print the result.

Exercises the exact code path the UI's "刷新行情数据" button takes
(`LiveEngine.refresh_market_data`), which is incremental -- see the docstring
there for why a full re-download is not acceptable as a button.

  python scripts/refresh_data_once.py [bar] [end]
"""
from __future__ import annotations

import json
import sys

from crypto_ls_research.execution.engine import LiveEngine


def main() -> None:
    bar = sys.argv[1] if len(sys.argv) > 1 else "1h"
    end = sys.argv[2] if len(sys.argv) > 2 else None
    out = LiveEngine.refresh_market_data(bar=bar, end=end, progress=print)
    print("\nRESULT " + json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
