"""Excel workbook export: Invoices / LineItems / NeedsReview sheets."""

from pathlib import Path

import pandas as pd

from invoice_extractor.pipeline import InvoiceResult
from invoice_extractor.schema import HEADER_FIELDS, LINE_ITEM_FIELDS

INVOICE_COLUMNS = (
    ["invoice_id"]
    + HEADER_FIELDS
    + ["source_file", "page_count", "extraction_method", "needs_review", "review_reason"]
)


def export_workbook(results: list[InvoiceResult], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    invoice_rows = []
    line_item_rows = []
    for i, res in enumerate(results, start=1):
        invoice_id = f"INV-{i:04d}"
        row = {"invoice_id": invoice_id}
        row.update({f: res.data.get(f) for f in HEADER_FIELDS})
        row.update(
            source_file=res.source_file,
            page_count=res.page_count,
            extraction_method=res.extraction_method,
            needs_review=res.needs_review,
            review_reason=res.review_reason,
        )
        invoice_rows.append(row)

        for j, item in enumerate(res.data.get("line_items") or [], start=1):
            li = {"invoice_id": invoice_id, "line_number": j, "source_file": res.source_file}
            li.update({f: item.get(f) for f in LINE_ITEM_FIELDS})
            line_item_rows.append(li)

    invoices_df = pd.DataFrame(invoice_rows, columns=INVOICE_COLUMNS)
    line_items_df = pd.DataFrame(
        line_item_rows,
        columns=["invoice_id", "line_number", "source_file"] + LINE_ITEM_FIELDS,
    )
    needs_review_df = invoices_df[invoices_df["needs_review"] == True]  # noqa: E712

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        invoices_df.to_excel(writer, sheet_name="Invoices", index=False)
        line_items_df.to_excel(writer, sheet_name="LineItems", index=False)
        needs_review_df.to_excel(writer, sheet_name="NeedsReview", index=False)

        # Reasonable column widths for quick triage.
        for sheet_name, df in (
            ("Invoices", invoices_df),
            ("LineItems", line_items_df),
            ("NeedsReview", needs_review_df),
        ):
            ws = writer.sheets[sheet_name]
            for col_idx, col_name in enumerate(df.columns, start=1):
                lengths = [
                    len(str(v)) for v in df[col_name].head(200) if not pd.isna(v)
                ]
                max_len = max([len(str(col_name))] + lengths)
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 2, 60)

    return output_path
