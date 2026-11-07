"""Shared staging helper for the two Lambdas that import the real
`outbound_ivr.tools` package (`lambdas/tools` behind the Gateway, `lambdas/api`
behind the dashboard's HTTP API) -- see lambdas/README.md and
lambdas/api/README.md's "Packaging" sections: "import outbound_ivr.tools, don't
vendor it", via a bundling command that copies the package in. Done here
in plain Python (``shutil.copy2``) at synth time rather than a Docker-based
CDK ``bundling`` block, since the whole operation is copying four small
stdlib + boto3 files into a throwaway staging directory -- nothing here
needs a container to assemble.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile


def _vendor_installed_package(name: str, staging_dir: str) -> None:
    """Copy an already-installed pure-Python package from *this* process's own
    environment into ``staging_dir``, so a Lambda bundled by plain file copy (no
    Docker, no `pip install` at synth time -- see this module's docstring) still
    has it at runtime. No network and no container: whatever `uv sync` already
    resolved locally (same pin as this project's own `pyproject.toml`) is what
    gets copied, so synth and the unit tests that call this function never need
    either."""
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"{name!r} is not installed in this environment; run `uv sync` first")
    package_dir = os.path.dirname(spec.origin)
    shutil.copytree(package_dir, os.path.join(staging_dir, name))


# Name and `outbound_ivr_package_dir` parameter deliberately NOT renamed:
# gateway_stack.py and dashboard_construct.py both import this exact module and
# call this exact function by name, to keep every Lambda this app bundles
# identically.
def build_outbound_ivr_tools_lambda_asset(
    lambda_dir: str,
    outbound_ivr_package_dir: str,
    extra_files: tuple[str, ...] = (),
    *,
    include_structlog: bool = False,
) -> str:
    """Stage ``handler.py`` (+ any ``extra_files``, e.g. ``schema.json``)
    from ``lambda_dir``, plus the real ``outbound_ivr.tools`` package copied in
    next to it, into a new staging directory, and return its path.

    ``include_structlog=True`` additionally vendors the installed `structlog`
    package (see `_vendor_installed_package`) -- for the dashboard API Lambda,
    which uses it for logging; the tools Lambda behind the Gateway does not log
    at all, so its own call leaves this off rather than bloat its package for
    nothing.
    """
    staging_dir = tempfile.mkdtemp(prefix="outbound-ivr-lambda-")
    for name in ("handler.py", *extra_files):
        shutil.copy2(os.path.join(lambda_dir, name), os.path.join(staging_dir, name))

    # Must be named "outbound_ivr", not "outbound-ivr": handler.py imports `outbound_ivr.tools...`
    # (see this module's own docstring), so the deployment package's root
    # package directory has to match that import exactly.
    package_root = os.path.join(staging_dir, "outbound_ivr")
    os.makedirs(os.path.join(package_root, "tools"), exist_ok=True)
    shutil.copy2(
        os.path.join(outbound_ivr_package_dir, "__init__.py"),
        os.path.join(package_root, "__init__.py"),
    )
    for name in ("__init__.py", "business.py", "store.py"):
        shutil.copy2(
            os.path.join(outbound_ivr_package_dir, "tools", name),
            os.path.join(package_root, "tools", name),
        )
    if include_structlog:
        _vendor_installed_package("structlog", staging_dir)
    return staging_dir
