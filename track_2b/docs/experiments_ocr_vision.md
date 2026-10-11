# Experiments: invoice files, OCR and Apertus reading images (11 October 2026)

Notes for the technical report. Numbers come from `results/ocr_study.json` and `results/vision_study.json`.

## Why

Real invoices arrive as PDFs, scans and photos, not as text. Apertus 1.5 takes images as input
(model card, huggingface.co/swiss-ai/Apertus-v1.5-8B: "The model takes images, audio, and text as input and
generates text"). The checks compare what Apertus read with the invoice itself, so they need a text that
Apertus did not produce. Design: Tesseract OCR gives that text; Apertus reads the image; code compares the two.

## Setup

- Invoices: confirm set (`data/study_confirm.jsonl`), random sample with seed 7.
- Images: the invoice text drawn black on white with Pillow's default font at about 200 dpi. "Scan-like":
  rotated by up to ±1.2°, Gaussian blur 0.9, JPEG quality 55. Not real scans.
- OCR: Tesseract 5.4.0, languages deu+fra+ita+eng, page mode 6, run locally.
- Apertus: swiss-ai/apertus-v1.5-8b via the Public AI API, the extraction prompt with the image attached,
  temperature 0. Responses are recorded in `data/cache/apertus_responses.jsonl` (tag `vision:`).
- A value counts as read if it is exactly right (IBAN after removing spaces, total to the cent, invoice number).
  Values the invoice itself does not show (unreadable total, pay-by-slip, missing number) are left out.

## Result 1: OCR, 300 invoices (`python -m agent ocr`)

| | IBAN | invoice no. | total | all three |
|---|---|---|---|---|
| clean image | 96.9% | 100% | 100% | 97.0% (9 failed, all IBAN) |
| scan-like image | 94.1% | 100% | 99.7% | 94.0% (18 failed: 17 IBAN, 1 total) |

A misread IBAN that still passes the IBAN checksum: 0 in both. A misread account is rejected or held, never
paid silently.

A first run counted invoices whose total was deliberately unreadable as OCR errors (all three 93.3% / 90.3%);
the metric was corrected and the study run again.

## Result 2: Apertus reads the image, 50 invoices (`python -m agent vision`)

The first 50 of the same sample, clean images.

- 40 answered, 10 failed. First pass (4 calls at a time): 24 answered, 26 failed (20 HTTP 504 timeouts,
  6 HTTP 400 "Unsupported model or no available clients", i.e. the 8B model was not available on the endpoint
  at the time). Retry of the 26 (2 at a time): 16 answered, 10 failed with HTTP 400.
  A failed call holds the invoice (fail closed).
- Of the 40 answered, 38 had at least one payment value wrong: IBAN 32, total 25, invoice number 23.
  2 were fully right (C024, C277). One observed case (C303): the total was taken from the UID number
  (CHE-727.203.150 -> 727.20).
- All 38 misread invoices were held by the code checks against the OCR text. 0 were paid.
- Tesseract on the same 50 images: all three values right for 92%.

## What it means, and what it does not

- On these images, Apertus 8B reads the invoice's meaning but not its numbers reliably; OCR reads the numbers.
  The checks catch the gap because they compare against a second, independent reading.
- Small sample (40 answered), synthetic images, one prompt, one model size; the 70B model was not tried.
- These runs are not part of the replayed evaluation (`eval`, `holdout`): the reference numbers (0 of 300)
  are on text invoices.

## Where it is in the code

- `src/agent/ocr.py`: `read()` (PDF text layer or OCR), `vision_extract()`, `measure()`, `measure_vision()`.
- `src/agent/app.py`: `POST /api/clients/{cid}/intake` (file in, OCR, Apertus on the image, checks on the OCR text).
- `src/agent/basis.py`: `of_intake()` adds the `INVOICE TEXT · OCR` source tag.
- `Dockerfile`: tesseract-ocr with deu, fra, ita.
