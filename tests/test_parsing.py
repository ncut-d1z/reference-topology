"""Meaningful parsing fixtures use generated PDFs, never private user papers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas

from reference_topology.matching import match_reference, normalize_arxiv, normalize_doi, normalize_title
from reference_topology.parsing import ParseError, parse_pdf, parse_reference_text


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_pdf(self, name="paper.pdf", title="Local citation evidence", own_doi=True):
        path = self.directory / name
        pdf = canvas.Canvas(str(path), pagesize=(612, 792))
        pdf.setTitle(title)
        pdf.setAuthor("Alice Example; Bob Example")
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(50, 730, title)
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, 705, "Alice Example and Bob Example")
        pdf.drawString(50, 688, "Published in Journal of Evidence, 2024")
        if own_doi:
            pdf.drawString(50, 671, "doi: 10.1234/document")
        pdf.drawString(50, 645, "Abstract")
        pdf.drawString(50, 625, "We study citations and retain the original evidence.")
        pdf.showPage()
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(50, 735, "References")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, 710, "[1] J. Smith. Reliable citation graphs.")
        pdf.drawString(68, 694, "Journal of Evidence 4 (2021). https://doi.org/10.1234/reference-one.")
        pdf.drawString(50, 665, "[2] Doe, A. (2022). Better local matching. arXiv:2201.12345v2.")
        pdf.drawString(50, 600, "Appendix A")
        pdf.drawString(50, 580, "[999] This appendix list is not a reference. doi:10.1234/not-a-reference")
        pdf.save()
        return path

    def test_own_metadata_doi_is_separate_from_reference_doi(self):
        result = parse_pdf(self.make_pdf())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["metadata"]["title"], "Local citation evidence")
        self.assertEqual(result["metadata"]["doi"], "10.1234/document")
        self.assertEqual(result["metadata"]["year"], 2024)
        self.assertEqual(len(result["metadata"]["authors"]), 2)
        self.assertEqual(len(result["references"]), 2)
        first, second = result["references"]
        self.assertEqual(first["parsed"]["doi"], "10.1234/reference-one")
        self.assertEqual(first["parsed"]["title"], "Reliable citation graphs")
        self.assertEqual(second["parsed"]["arxiv"], "2201.12345")
        self.assertEqual(second["parsed"]["arxiv_version"], 2)
        self.assertEqual({item["page"] for item in first["locations"]}, {2})
        self.assertIn("\n", first["raw_text"])
        self.assertNotIn("not-a-reference", " ".join(item["raw_text"] for item in result["references"]))
        self.assertEqual(result["page_count"], 2)
        self.assertIn("pdfminer.six=", result["engine_version"])

    def test_reference_doi_never_fills_missing_document_doi(self):
        result = parse_pdf(self.make_pdf(own_doi=False))
        self.assertIsNone(result["metadata"]["doi"])
        self.assertEqual(result["references"][0]["parsed"]["doi"], "10.1234/reference-one")

    def test_references_on_first_page_do_not_supply_document_identifiers(self):
        path = self.directory / "one-page.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.setTitle("One page bibliography")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(50, 780, "One page bibliography")
        pdf.drawString(50, 740, "References")
        pdf.drawString(50, 710, "[1] Smith, J. (2020). A cited paper. doi:10.1234/cited")
        pdf.save()
        result = parse_pdf(path)
        self.assertIsNone(result["metadata"]["doi"])
        self.assertEqual(result["references"][0]["parsed"]["doi"], "10.1234/cited")

    def test_two_columns_and_cross_page_continuations_keep_order_and_evidence(self):
        path = self.directory / "columns.pdf"
        pdf = canvas.Canvas(str(path), pagesize=(612, 792))
        pdf.setTitle("Column reading order")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 770, "Repeated journal header")
        pdf.drawString(40, 720, "Column reading order")
        pdf.drawString(40, 700, "This document demonstrates reference evidence across pages.")
        pdf.showPage()
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 770, "Repeated journal header")
        pdf.drawString(40, 740, "References")
        pdf.drawString(40, 710, "[1] Smith, J. (2020). First paper.")
        pdf.drawString(55, 695, "doi:10.1234/one")
        pdf.drawString(40, 665, "[2] Doe, A. (2021). Second paper.")
        pdf.drawString(55, 650, "doi:10.1234/two")
        pdf.drawString(330, 710, "[3] Jones, B. (2022). Third paper.")
        pdf.drawString(345, 695, "doi:10.1234/three")
        pdf.drawString(330, 665, "[4] Brown, C. (2023). Fourth paper.")
        pdf.drawString(345, 650, "A reference continued on the next page,")
        pdf.drawString(300, 20, "2")
        pdf.showPage()
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 770, "Repeated journal header")
        pdf.drawString(55, 725, "doi:10.1234/four")
        pdf.drawString(40, 680, "[5] Green, D. (2024). Fifth paper. doi:10.1234/five")
        pdf.drawString(300, 20, "3")
        pdf.save()
        result = parse_pdf(path)
        self.assertEqual([item["label"] for item in result["references"]], ["[1]", "[2]", "[3]", "[4]", "[5]"])
        fourth = result["references"][3]
        self.assertEqual(fourth["parsed"]["doi"], "10.1234/four")
        self.assertEqual({item["page"] for item in fourth["locations"]}, {2, 3})
        self.assertNotIn("Repeated journal header", fourth["raw_text"])
        self.assertTrue(any(warning.startswith("two_column_layout") for warning in result["warnings"]))

    def test_author_year_entries_are_split_without_numbers(self):
        path = self.directory / "author-year.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.setTitle("Author year examples")
        pdf.setFont("Helvetica", 11)
        for y, text in ((780, "Author year examples"), (730, "References"),
                        (700, "Smith, J. (2020). Graph construction. doi:10.1234/graph"),
                        (670, "Jones, K. and Brown, A. (2021). Database evidence."),
                        (650, "Journal of Local Research. doi:10.1234/data")):
            pdf.drawString(50, y, text)
        pdf.save()
        result = parse_pdf(path)
        self.assertEqual(len(result["references"]), 2)
        self.assertEqual(result["references"][1]["parsed"]["title"], "Database evidence")
        self.assertEqual(result["references"][1]["parsed"]["year"], 2021)

    def test_missing_heading_is_partial_and_cannot_auto_match(self):
        path = self.directory / "headingless.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.setTitle("Headingless bibliography")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, 780, "Headingless bibliography")
        pdf.drawString(50, 700, "[1] Smith, J. (2020). Graph construction. doi:10.1234/graph")
        pdf.drawString(50, 670, "[2] Jones, A. (2021). Other citation. doi:10.1234/other")
        pdf.save()
        result = parse_pdf(path)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["references"][0]["parsed"]["_needs_review"])
        candidates = match_reference(result["references"][0]["parsed"], [{"id": "p1", "doi": "10.1234/graph"}])
        self.assertFalse(candidates[0]["safe"])

    def test_text_without_bibliography_is_not_reported_as_complete(self):
        path = self.directory / "no-bibliography.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.setFont("Helvetica", 12)
        pdf.drawString(50, 740, "This text is readable but its bibliography cannot be located.")
        pdf.save()
        result = parse_pdf(path)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["references"], [])
        self.assertTrue(any(w.startswith("reference_section_not_found") for w in result["warnings"]))

    def test_image_only_document_needs_ocr(self):
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        path = self.directory / "scan.pdf"
        pdf = canvas.Canvas(str(path))
        pdf.drawImage(ImageReader(Image.new("RGB", (100, 100), "gray")), 40, 40, width=500, height=700)
        pdf.save()
        result = parse_pdf(path)
        self.assertEqual(result["status"], "needs_ocr")
        self.assertEqual(result["references"], [])
        self.assertTrue(any(w.startswith("no_extractable_text") for w in result["warnings"]))

    def test_page_limit_and_unknown_engine_have_explicit_errors(self):
        path = self.make_pdf()
        with self.assertRaises(ParseError) as caught:
            parse_pdf(path, max_pages=1)
        self.assertEqual(caught.exception.code, "page_limit_exceeded")
        with self.assertRaises(ParseError) as caught:
            parse_pdf(path, engine="cloud-upload")
        self.assertEqual(caught.exception.code, "unknown_engine")

    def test_grobid_is_local_and_consolidation_is_disabled(self):
        path = self.make_pdf()
        received = {}
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader><fileDesc><titleStmt><title>Own title</title></titleStmt>
            <sourceDesc><biblStruct><analytic><title>Own title</title>
              <author><persName><forename>Alice</forename><surname>Example</surname></persName></author>
              <idno type="DOI">10.1234/own-xml</idno></analytic>
              <monogr><imprint><date when="2024"/></imprint></monogr></biblStruct></sourceDesc>
          </fileDesc></teiHeader>
          <text><back><listBibl><biblStruct n="[1]" coords="2,50,70,300,25">
            <analytic><title>Cited title</title><idno type="DOI">10.1234/cited-xml</idno></analytic>
            <monogr><imprint><date when="2020"/></imprint></monogr>
            <note type="raw_reference">Smith (2020). Cited title. doi:10.1234/cited-xml</note>
          </biblStruct></listBibl></back></text>
        </TEI>'''.encode()

        class Response(BytesIO):
            status = 200

        class LocalTransport:
            def open(self, request, timeout):
                if isinstance(request, str):
                    self_url = request
                    if self_url != "http://127.0.0.1:8070/api/version":
                        raise AssertionError("Unexpected service URL")
                    return Response(b"test-service-1")
                received["path"] = request.full_url
                received["body"] = request.data
                return Response(xml)

        with patch("reference_topology.parsing.build_opener", return_value=LocalTransport()):
            result = parse_pdf(path, engine="grobid-local")
        self.assertEqual(received["path"], "http://127.0.0.1:8070/api/processFulltextDocument")
        for field in ("consolidateHeader", "consolidateCitations", "consolidateFunders"):
            self.assertIn(f'name="{field}"\r\n\r\n0\r\n'.encode(), received["body"])
        self.assertIn(b'name="includeRawCitations"\r\n\r\n1\r\n', received["body"])
        self.assertEqual(result["metadata"]["doi"], "10.1234/own-xml")
        self.assertEqual(result["references"][0]["parsed"]["doi"], "10.1234/cited-xml")
        self.assertEqual(result["references"][0]["locations"][0]["page"], 2)
        self.assertEqual(result["raw_output"]["text"], xml.decode())
        self.assertIn("test-service-1", result["engine_version"])
        with self.assertRaises(ParseError) as caught:
            parse_pdf(path, engine="grobid-local", grobid_url="https://example.org")
        self.assertEqual(caught.exception.code, "grobid_not_local")

    def test_worker_writes_structured_failure_without_echoing_input(self):
        source = self.directory / "invalid.pdf"
        source.write_text("Private raw content must never appear on stdout.")
        output = self.directory / "result.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run([sys.executable, "-m", "reference_topology.parse_worker",
                                    "--input", str(source), "--output", str(output)],
                                   env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 1)
        result = json.loads(output.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_pdf")
        self.assertNotIn("Private raw content", completed.stdout + completed.stderr)

    def test_worker_runs_full_parser_and_writes_a_reopenable_result(self):
        source = self.make_pdf()
        output = self.directory / "success.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run([sys.executable, "-m", "reference_topology.parse_worker",
                                    "--input", str(source), "--output", str(output), "--timeout", "10"],
                                   env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(output.read_text())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["references"]), 2)
        self.assertEqual(json.loads(completed.stdout)["status"], "succeeded")
        self.assertNotIn("Reliable citation graphs", completed.stdout + completed.stderr)


class MatchingTests(unittest.TestCase):
    def test_identifier_normalization_handles_punctuation_and_versions(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1234/ABC(DEF)."), "10.1234/abc(def)")
        self.assertEqual(normalize_doi("(doi:10.1234/Example)."), "10.1234/example")
        self.assertEqual(normalize_doi("https://doi.org/10.1234/a%28b%29?source=web"), "10.1234/a(b)")
        self.assertIsNone(normalize_doi("not a DOI"))
        self.assertEqual(normalize_arxiv("arXiv:2201.12345v2"), "2201.12345")
        self.assertEqual(normalize_arxiv("https://arxiv.org/abs/hep-th/9901001v3"), "hep-th/9901001")
        self.assertIsNone(normalize_arxiv("Number 2020.12345 is not an identifier"))
        self.assertEqual(normalize_title("Étude of cross-\nreference graphs!"), "etude of crossreference graphs")

    def test_safe_identifier_match_accepts_identifier_list_schema(self):
        paper = {"id": "paper-1", "title": "Reliable citation graphs",
                 "identifiers": [{"scheme": "doi", "value_normalized": "10.1234/one"}]}
        result = match_reference({"doi": "https://doi.org/10.1234/ONE", "title": "Reliable citation graphs"}, [paper])
        self.assertEqual(result[0]["method"], "doi")
        self.assertTrue(result[0]["safe"])

    def test_ambiguous_identifiers_are_never_safe(self):
        papers = [{"id": "one", "doi": "10.1234/one"}, {"id": "two", "doi": "10.1234/one"}]
        result = match_reference({"doi": "10.1234/one"}, papers)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(not candidate["safe"] for candidate in result))

    def test_identifier_and_title_conflicts_require_review(self):
        result = match_reference({"doi": "10.1234/one", "title": "Visual odometry for flying robots"},
                                 [{"id": "one", "doi": "10.1234/one", "title": "Medieval poetry in northern Europe"}])
        self.assertFalse(result[0]["safe"])
        self.assertIn("different titles", result[0]["reason"])
        result = match_reference({"doi": "10.1234/preprint", "arxiv": "2201.12345"},
                                 [{"id": "one", "doi": "10.1234/published", "arxiv": "2201.12345"}])
        self.assertFalse(result[0]["safe"])

    def test_exact_and_fuzzy_titles_remain_candidates(self):
        result = match_reference({"title": "Reliable citation graphs"},
                                 [{"id": "exact", "title": "Reliable Citation Graphs"},
                                  {"id": "near", "title": "Reliable citation graph"}])
        self.assertEqual(result[0]["method"], "title-exact")
        self.assertTrue(all(not candidate["safe"] for candidate in result))

    def test_raw_reference_title_hyphenation_and_author_year(self):
        parsed = parse_reference_text("[12] Smith, J. (2020). Local cross-\nreference graphs. Journal of Methods. DOI:10.1234/graph")
        self.assertEqual(parsed["title"], "Local crossreference graphs")
        self.assertEqual(parsed["year"], 2020)
        self.assertEqual(parsed["doi"], "10.1234/graph")
        self.assertEqual(parsed["authors"], [{"display_name": "Smith, J."}])

    def test_surname_initial_authors_preserve_order_and_pairs(self):
        parsed = parse_reference_text("Smith, J., Jones, K. and Brown, A. (2021). Database evidence. doi:10.1234/data")
        self.assertEqual([author["display_name"] for author in parsed["authors"]], ["Smith, J.", "Jones, K.", "Brown, A."])


if __name__ == "__main__":
    unittest.main()
