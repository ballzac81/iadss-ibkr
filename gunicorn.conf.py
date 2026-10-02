"""
gunicorn.conf.py
Gunicorn configuration for IADSS-IBKR.

IMPORTANT: workers must stay at 1.
ib_insync starts a background event loop thread; multiple workers would
each start their own monitor and IB connection, causing duplicate orders.
"""

import asyncio

bind    = "0.0.0.0:5005"
workers = 1        # Do NOT increase — see above
threads = 4
timeout = 120
loglevel = "info"
accesslog = "-"    # Log requests to stdout


def post_fork(server, worker):
    """
    Give the worker process a clean asyncio event loop after Gunicorn forks.
    Without this, ib_insync's util.startLoop() can inherit a broken loop
    from the parent process if --preload is ever used.
    """
    asyncio.set_event_loop(asyncio.new_event_loop())
