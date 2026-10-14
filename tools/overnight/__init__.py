"""Overnight investigation batch: queue short requests, collect results, write one morning report.

See README.md in this directory. Nothing here queries a database or kills a process: requests
are handed to the fleet through the cockpit route and results are read back from the fleet's
own state files.
"""
