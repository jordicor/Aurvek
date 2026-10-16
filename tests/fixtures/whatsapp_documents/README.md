# WhatsApp document fixtures

`report.pdf`, `report.docx`, `sales.xlsx`, and `report.pptx` are synthetic files
created for this regression. They contain the project marker `violet-73`, the
name `Aurvek`, and revenue `42`. The spreadsheet has two sheets and a formula
with cached result `84`. They were generated with PyMuPDF, python-docx,
XlsxWriter, and python-pptx; those generators are not needed to run the tests.

The three unchanged legacy files come from the Apache POI test corpus,
downloaded on 2026-10-14. Apache's license and notice accompany them.

| Local file | Upstream path under https://github.com/apache/poi/tree/trunk/test-data | SHA-256 |
| --- | --- | --- |
| legacy.doc | document/simple.doc | 4876e828e62d490fa188c7c4e47013e2e0dfdf02f5903dfd374bc0d2b2049ca1 |
| legacy.xls | spreadsheet/Simple.xls | 2b5ec8298bacffdca61c50f6f7607da0a4e63cf76fa011c62f203e6a66bc072a |
| legacy.ppt | slideshow/basic_test_ppt_file.ppt | e8fd4c38ad72ce3735acbca0b60dd5870fb60f819c169e3947d166f4f1bcc79d |
