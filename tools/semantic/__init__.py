# -*- coding: utf-8 -*-
"""Generic semantic layer: a glossary, metric definitions and a table catalogue.

The mechanism lives here. The content (terms, metrics, table descriptions) is supplied by the
operator as JSON files in a folder named by the MCP_SEMANTIC_DIR environment variable, outside
this repository. See README.md in this folder.

Submodules (import them explicitly):
    store      load + validate + cache the JSON files
    resolve    find defined terms / metrics in a request, list the undefined words
    metric_sql turn a metric into SQL with bind variables; resolve relative dates
    catalog    search / describe / join-path over the table catalogue
    ops        the MCP tool entry points (gated)
"""
