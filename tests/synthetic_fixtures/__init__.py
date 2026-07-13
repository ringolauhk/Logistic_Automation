"""Synthetic invoice validation pack: ground truth and (later) PDF builders.

This package must never import provider SDK clients (google.genai, anthropic)
or invoice_extractor's runtime pipeline modules. Ground truth is hand-authored
and independent of the application under test - see ground_truth.py.
"""
