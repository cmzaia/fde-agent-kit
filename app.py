"""Vercel entrypoint: exposes the demo dashboard as `app`."""

import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
for _p in (_root, os.path.join(_root, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fde_kit.demo_app import create_demo_app  # noqa: E402

app = create_demo_app()
