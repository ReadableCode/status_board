# %%
# Imports #

import os
import platform
import socket

# %%
# Functions #


def get_uppercase_hostname():
    """
    The local machine's uppercase hostname: the HOSTNAME env var when set,
    else COMPUTERNAME on Windows, else socket.gethostname(). Used to decide
    whether the board is already running ON a panel's jump host (in which
    case the -J hop is skipped), so only the short pre-dot name matters to
    callers.
    """
    hostname = os.getenv("HOSTNAME")
    if hostname is None and platform.system().upper() == "WINDOWS":
        hostname = os.environ.get("COMPUTERNAME")
    if hostname is None:
        hostname = socket.gethostname()
    return hostname.upper() if hostname else ""


# %%
