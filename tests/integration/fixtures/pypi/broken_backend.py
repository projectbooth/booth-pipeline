"""The build backend of the ``booth-pip-broken`` sdist: its build fails on purpose (ADR 0115
Integration). The marker must appear in the task's failure message, so the test knows the message is
pip's own account of THIS build, not a generic error."""

import sys

MARKER = "BOOTH-PIP-BROKEN: this sdist's build fails on purpose"


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    print(MARKER, flush=True)
    sys.exit(1)
