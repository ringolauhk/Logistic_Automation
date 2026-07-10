# Sample invoices

Two generated fixtures are already here so you can test both pipeline paths
immediately (`fixture_text_invoice.pdf` is text-native; `fixture_scanned_invoice.pdf`
is a pure image with no text layer). Delete them once you have real samples.

Drop your sample invoice PDFs into this folder — any mix of:

- born-digital PDFs (clean text layer)
- previously OCR'd PDFs
- pure scanned-image PDFs (no text layer)

Then run either:

```
python -m invoice_extractor run --input ./samples --output ./output/results.xlsx
```

or the smoke test:

```
python test_pipeline.py
```

Tip: run `python -m invoice_extractor classify --input ./samples` first to see
how each file will be routed (text vs vision) without spending any API tokens.
