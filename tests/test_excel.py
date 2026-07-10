from decimal import Decimal

import openpyxl
import pandas as pd

from invoice_extractor.excel_export import export_workbook
from invoice_extractor.pipeline import InvoiceResult
from invoice_extractor.schema import empty_invoice, normalize_invoice

from .conftest import invoice_dict


def sample_results():
    ok = InvoiceResult(
        source_file="good.pdf",
        invoice=normalize_invoice(invoice_dict()),
        page_count=1,
        document_classification="text-native",
        extraction_method="text",
        provider="gemini",
        model="gemini-test-text",
        text_pages=[1],
    )
    flagged = InvoiceResult(
        source_file="flagged.pdf",
        invoice=normalize_invoice(invoice_dict(total_amount=999.0)),
        page_count=2,
        document_classification="mixed",
        extraction_method="mixed",
        provider="mixed",
        model="gemini-test-text+claude-test-vision",
        text_pages=[1, 2, 5, 6, 7],
        image_pages=[3],
        failed_pages=[4],
        vision_chunk_count=1,
        needs_review=True,
        review_reason="totals inconclusive: synthetic",
    )
    failed = InvoiceResult(  # all-null row: both providers failed
        source_file="failed.pdf",
        invoice=empty_invoice(),
        page_count=1,
        document_classification="image-only",
        extraction_method="failed",
        provider="none",
        model=None,
        image_pages=[1],
        needs_review=True,
        error=True,
        review_reason="vision route failed on all providers: synthetic",
    )
    return [ok, flagged, failed]


class TestWorkbookStructure:
    def test_exactly_three_sheets(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames == ["Invoices", "LineItems", "NeedsReview"]

    def test_reopens_with_openpyxl_and_pandas(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        openpyxl.load_workbook(path).close()
        for sheet in ("Invoices", "LineItems", "NeedsReview"):
            pd.read_excel(path, sheet_name=sheet)

    def test_provenance_columns_present(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        df = pd.read_excel(path, sheet_name="Invoices")
        for col in ("document_classification", "extraction_method", "provider",
                    "model", "text_pages", "image_pages", "blank_pages",
                    "failed_pages", "vision_chunk_count"):
            assert col in df.columns
        assert set(df["extraction_method"]) == {"text", "mixed", "failed"}
        assert set(df["provider"]) == {"gemini", "mixed", "none"}

    def test_page_columns_use_human_readable_ranges(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        # assert on raw openpyxl cells (pandas read_excel would type-infer "4" -> 4.0)
        ws = openpyxl.load_workbook(path)["Invoices"]
        headers = [c.value for c in ws[1]]
        row = next(r for r in ws.iter_rows(min_row=2)
                   if r[headers.index("source_file")].value == "flagged.pdf")
        assert row[headers.index("text_pages")].value == "1-2,5-7"  # documented format
        assert row[headers.index("failed_pages")].value == "4"
        assert row[headers.index("vision_chunk_count")].value == 1
        assert "[" not in str(row[headers.index("text_pages")].value)  # no list syntax


class TestReferentialIntegrity:
    def test_line_items_reference_existing_invoices(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        invoices = pd.read_excel(path, sheet_name="Invoices")
        items = pd.read_excel(path, sheet_name="LineItems")
        assert not items.empty
        assert set(items["invoice_id"]).issubset(set(invoices["invoice_id"]))

    def test_needs_review_is_exact_subset(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "out.xlsx")
        invoices = pd.read_excel(path, sheet_name="Invoices")
        review = pd.read_excel(path, sheet_name="NeedsReview")
        assert bool(review["needs_review"].all())
        expected = set(invoices[invoices["needs_review"]]["invoice_id"])
        assert set(review["invoice_id"]) == expected


class TestEdgeCases:
    def test_no_line_items_still_valid_workbook(self, tmp_path):
        failed_only = [sample_results()[2]]
        path = export_workbook(failed_only, tmp_path / "empty_items.xlsx")
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames == ["Invoices", "LineItems", "NeedsReview"]
        items = pd.read_excel(path, sheet_name="LineItems")
        assert items.empty

    def test_null_values_do_not_break_column_sizing(self, tmp_path):
        # failed row is all-None across header fields
        path = export_workbook(sample_results(), tmp_path / "nulls.xlsx")
        assert path.exists()

    def test_decimals_exported_as_numeric_cells(self, tmp_path):
        path = export_workbook(sample_results(), tmp_path / "decimals.xlsx")
        wb = openpyxl.load_workbook(path)
        ws = wb["Invoices"]
        headers = [c.value for c in ws[1]]
        total_col = headers.index("total_amount") + 1
        cell = ws.cell(row=2, column=total_col)  # the "good.pdf" row
        assert isinstance(cell.value, (int, float))
        assert not isinstance(cell.value, str)
        assert abs(cell.value - float(Decimal("119.0"))) < 1e-9
