"""Transfer Note Packing List workflow (feature-flagged, Build 1).

Isolated from the invoice-extraction workflow: its own job type, id format,
jobs root, metadata schema, and session-state keys. Only generic
infrastructure (filename sanitizing, atomic JSON writes, UI style, PDF page
inspection) is shared with the invoice pilot.
"""
