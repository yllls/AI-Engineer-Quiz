"""Shared test environment.

Set the data-directory override before any test module imports ``keepstack``.
The suite intentionally shares one throwaway catalog; individual tests use
unique names/prefixes so they do not depend on a clean database.
"""
import os
import tempfile

os.environ.setdefault(
    "KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-")
)
os.environ.setdefault("KEEPSTACK_AI_ENABLED", "false")
