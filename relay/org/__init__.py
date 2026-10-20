"""Versioned record of the roles and protocol text the fleet runs under (slices D1 and D2).

Nothing in this package changes what a worker is told or which goal is admitted. It reads the
role and protocol definitions where they already live, hashes them, and (setting
`org_version_record`, default off) writes one line per run so runs can be grouped by version.
"""
