"""Where things are. This repo (PVDX-flatsat-hil) is a submodule of PVDX, mounted at PVDX/flatsat-hil; the suite reads
the flatsat's sources and ELF from PVDX. Set PVDX_ROOT to use it from somewhere else."""

import os

HIL_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PVDX = os.path.normpath(os.environ.get("PVDX_ROOT") or os.path.join(HIL_REPO, ".."))
