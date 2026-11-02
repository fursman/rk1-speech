#!/usr/bin/env python3
"""Set up an RK3588 board as a speech appliance: ./install.py --dry-run, then ./install.py.
See rk1speech/installer.py and docs/INSTALL.md."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from rk1speech.installer import main  # noqa: E402

sys.exit(main())
