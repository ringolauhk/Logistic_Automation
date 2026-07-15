"""Offline ground-truth benchmark for the invoice extractor (M6).

Benchmark-only tooling: measures already-produced extraction output
(workbook + usage CSV + optional run metadata) against human-authored ground
truth. Makes ZERO provider/network calls and never influences extraction
decisions. The production pipeline never imports this package.
"""
