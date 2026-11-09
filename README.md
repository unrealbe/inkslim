# Inkslim

**Optimize EPUB and PDF files for e-readers — self-hosted, simple, and privacy-conscious.**

Inkslim is a small web application for reducing the size of EPUB books and PDF documents before transferring them to an e-reader. It is designed with devices such as the black-and-white Kobo Glo in mind, but includes presets for colour screens and lets you tune the settings.

It runs on your own NAS, Raspberry Pi, home server, or Linux machine. Files are processed locally by your Inkslim instance; there is no Inkslim cloud account or external conversion service.

## Contents

- [Features](#features)
- [Which mode should I use?](#which-mode-should-i-use)
- [Getting started with Docker](#getting-started-with-docker)
- [Updating Inkslim](#updating-inkslim)
- [Configuration](#configuration)
- [Using the web interface](#using-the-web-interface)
- [How processing works](#how-processing-works)
- [Command-line EPUB tool](#command-line-epub-tool)
- [HTTP API](#http-api)
- [Privacy and security](#privacy-and-security)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)
- [Project structure](#project-structure)
- [License](#license)

## Features

- **EPUB optimization:** resize and recompress JPEG and PNG images inside EPUB archives.
- **PDF optimization:** recompress and downsample supported embedded PDF images while keeping the PDF text and vector content intact.
- **PDF E-reader mode:** render each PDF page to an e-reader-sized image and rebuild the document as a compact, image-based PDF.
- **Mixed batches:** upload EPUB and PDF files together. Each file uses the settings for its own format.
- **E-ink presets:** Kobo Glo / black-and-white e-ink and colour-screen presets, plus custom values.
- **Grayscale conversion:** optional for EPUB images and supported PDF image processing.
- **Safe output handling:** original uploads are never overwritten. A processed file is only offered when it is valid and smaller than its input.
- **Temporary processing:** web uploads are processed in temporary directories, which are removed after the request.
- **Browser preferences:** EPUB and PDF settings are independent and saved in the current browser.
- **History controls:** clear completed or failed results without cancelling queued or running jobs.
- **Self-hosted:** Docker image for linux/amd64 and linux/arm64.
- **Optional HTTP Basic Authentication** and health-check endpoint.
- **PDF-to-EPUB conversion:** upload text-based or scanned PDFs and create reflowable EPUB 3 books using native text extraction and OCR. Jobs run in the background, can be monitored or cancelled, and provide a quality report with the generated EPUB.
- **Modern dark dashboard:** a responsive workspace with sidebar navigation, clear upload flow and conversion history.

## Which mode should I use?

| Input / goal | Recommended mode | Why |
|---|---|---|
| EPUB book with large photos or illustrations | **EPUB settings** | Shrinks supported images without changing the EPUB chapter structure. |
| Text-based PDF with embedded photographs | **Optimize PDF** | Keeps text selectable and searchable, and retains vector content. |
| Mixed or scanned PDF | **Try Optimize PDF first** | Compresses supported embedded images without rasterizing every page. |
| Scanned book or comic where page appearance matters most | **PDF E-reader mode** | Converts each page to an image sized for the target reader. |
| Text PDF that must remain searchable/selectable | **Do not use E-reader mode** | E-reader mode rasterizes the pages and removes the text layer. |

**Tip:** For a typical digital PDF, start with **Optimize PDF**. E-reader mode is more useful for scans and comics, but it can make a text-based PDF larger. Inkslim rejects an E-reader result that is not smaller than the original.

## Getting started with Docker

The repository's docker-compose.yml uses the published image from GitHub Container Registry (GHCR).

### Docker Compose (recommended)

    git clone https://github.com/unrealbe/inkslim.git
    cd inkslim

    # Pull the published image and start the service
    docker compose pull
    docker compose up -d

Open:

- On the Docker host: http://localhost:8125/
- From another device on your LAN: http://DOCKER-HOST-IP:8125/

The included Compose file maps **host port 8125** to **container port 8080**:

    ports:
      - "8125:8080"

The application listens on port 8080 inside the container. Change the number on the **left** to use a different host port; do not change the right-hand port unless you also change the container's Gunicorn bind address.

### Run the published image without Compose

    docker run -d \
      --name inkslim \
      --restart unless-stopped \
      -p 8125:8080 \
      ghcr.io/unrealbe/inkslim:latest

### Build a local development image

To build from the source in your working directory instead of pulling GHCR:

    docker build -t inkslim:local .
    docker run -d \
      --name inkslim \
      --restart unless-stopped \
      -p 8125:8080 \
      inkslim:local

A local build does not automatically update the published GHCR image. The published image is built by GitHub Actions when changes are pushed to main or a version tag is pushed.

## Updating Inkslim

For the standard GHCR Compose deployment:

    docker compose pull
    docker compose up -d --force-recreate

To follow the container logs:

    # Follow the web application
    docker compose logs -f web

    # Follow the background conversion worker
    docker compose logs -f worker

The Compose file on your NAS is a local file. Pulling a new image does **not** copy a changed docker-compose.yml from GitHub onto the NAS; update the local Compose file separately when its settings change. Keep the `conversion-data` volume: it contains the persistent SQLite database and conversion job workspaces. Back it up together with any other application data you want to retain.

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `JOB_RETENTION_HOURS` | `24` | Retain completed, failed and cancelled jobs for this many hours after completion. |
| `MAX_CONVERSION_STORAGE_MB` | `5120` | Admission quota for persistent conversion job workspaces; a new upload returns HTTP 507 when usage is over the limit. |

The supplied docker-compose.yml sets the host port mapping to 8125:8080 and configures the following environment variables.

| Variable | Default | Description |
|---|---:|---|
| MAX_UPLOAD_MB | 500 | Maximum size of one uploaded file, in MiB. |
| SHRINK_TIMEOUT | 840 | Timeout in seconds for the EPUB shell-script subprocess. |
| LOG_LEVEL | INFO | Python application log level, for example INFO, WARNING or DEBUG. |
| BASIC_AUTH_USER | empty | Optional HTTP Basic Authentication username. |
| BASIC_AUTH_PASS | empty | Optional HTTP Basic Authentication password. |
| PORT | 8080 | Internal listening port used by the Dockerfile's default command. |
| INKSLIM_DATA_DIR | /data/conversion in Compose | Persistent directory for the conversion queue, database and job workspaces. |
| QUEUE_POLL_SECONDS | 2 | Worker queue polling interval in seconds. |
| PDF_RENDER_DPI | 300 | Preferred DPI when rendering PDF pages for OCR. |
| MIN_RENDER_DPI | 120 | Minimum permitted PDF render DPI. |
| MAX_RENDER_PIXELS | 50000000 | Maximum pixel count for one rendered page. |
| MAX_RENDER_DIMENSION | 12000 | Maximum width or height, in pixels, for one rendered page. |
| NATIVE_TEXT_SAMPLE_INTERVAL | 10 | Validate every Nth eligible native-text page against OCR, starting at page 1. |
| OCR_TIMEOUT_PAGE | 120 | Maximum Tesseract runtime per page, in seconds. |
| OCR_WORKERS | 1 | Number of OCR workers. V1 intentionally requires exactly one. |
| MAX_OCR_PAGES | 0 | Maximum OCR page count; 0 means unlimited. |
| JOB_RETENTION_HOURS | 24 | Retain completed, failed and cancelled jobs for this many hours after completion. |
| MAX_CONVERSION_STORAGE_MB | 5120 | Admission quota for persistent conversion job workspaces; new uploads return HTTP 507 when full. |

Set both BASIC_AUTH_USER and BASIC_AUTH_PASS to enable login. If either is empty, authentication is disabled.

**Timeout note:** the supplied Compose file overrides the image's default Gunicorn command and sets Gunicorn's request timeout to 300 seconds. SHRINK_TIMEOUT is a separate timeout for the EPUB helper process. If you routinely process large files that take several minutes, keep the Gunicorn timeout and SHRINK_TIMEOUT aligned with the processing time you need.

### Port mapping

Docker port mappings use the format HOST_PORT:CONTAINER_PORT:

- 8125 is the port exposed on your NAS or server.
- 8080 is the port used by Inkslim inside the container.

For example, 8125:8080 makes Inkslim available at http://NAS-IP:8125/. The container's internal port remains 8080.

## Using the web interface

The web interface uses a responsive dark dashboard with navigation for the main workspace and conversion histories. The upload area is shared by both operations; choose whether to optimize files or convert PDFs to EPUB before adding files.

### EPUB settings

The EPUB panel contains the following controls:

| Setting | Kobo Glo preset | Colour preset | Meaning |
|---|---:|---:|---|
| Maximum width | 768 px | 1200 px | Maximum image width. |
| Maximum height | 1024 px | 1600 px | Maximum image height. |
| JPEG quality | 75 | 80 | JPEG output quality (1–100 in the CLI; 10–100 in the web form). |
| Minimum image size | 50 kB | 50 kB | Smaller images are left untouched. |
| Grayscale | On | Off | Convert supported images to grayscale. |

Images are fitted inside the selected width and height. They are never enlarged.

### PDF-to-EPUB conversion

Use the separate **Convert PDF to EPUB** panel for text-based PDFs and scanned books. Select one or more PDF files, choose the OCR language and submit. The conversion runs as a background job, so it does not hold the upload request open while OCR and EPUB generation run.

| Language | OCR selection |
|---|---|
| English (default) | English OCR |
| Dutch / French / German | Single-language OCR |
| Dutch + English | Both language models |
| French / German | Single-language OCR |
| French + English | Both language models |

The job list shows page progress and allows an active conversion to be cancelled. Completed jobs provide a reflowable EPUB and a JSON quality report. Recent job IDs are saved in the current browser so their status can be restored after returning to the page. The source PDF and other job files are retained in the conversion workspace for 24 hours after job completion by default. Expired completed, failed and cancelled jobs are removed automatically during periodic cleanup; queued and active jobs are not removed.

The generated EPUB contains reconstructed text, headings and chapter navigation. Extracted PDF images are placed inline after a source text block when page geometry provides a clear, bounded anchor. Images without a reliable anchor remain in a labelled Illustrations section at the end of the book. Semantic page classification is included in the quality report and guides conservative image handling: a first-page cover candidate is promoted only at classification confidence ≥0.75 with a dominant image covering ≥45% of the page; advertisement candidates at confidence ≥0.65 are retained in a separate Promotional material section rather than placed inline. No image is discarded; uncertain images keep the existing inline/Illustrations fallback. OCR is unchanged, and duplicate-cover removal remains future work.

### PDF settings

PDF settings are independent from EPUB settings.

#### Optimize PDF

This is the recommended starting point for ordinary digital PDFs.

| Setting | Default | Meaning |
|---|---:|---|
| Embedded image resolution | 150 DPI | Target pixel ceiling derived from the largest PDF page box. |
| Minimum image size | 50 kB | Smaller embedded image streams are skipped. |
| JPEG quality | 75 | Quality used when supported images are re-encoded. |
| Grayscale | Off | Convert supported images to grayscale. |

The optimizer considers embedded PDF image objects. It replaces a re-encoded image only when the new image stream is at least 10% smaller than the original stream. Text and vector content are not rasterized.

The DPI target is estimated from the largest page CropBox in the document. It is not a per-image effective-resolution calculation based on each image's placement on the page.

#### E-reader mode

E-reader mode renders every page to an image and inserts that image into a new PDF page.

| Setting | Default | Meaning |
|---|---:|---|
| Maximum width | 768 px | Maximum rendered page width. |
| Maximum height | 1024 px | Maximum rendered page height. |
| JPEG quality | 75 | JPEG quality for rendered pages. |
| Grayscale | Off | Render pages in grayscale. |

The page aspect ratio is fitted inside the selected dimensions. The output keeps the original page count and basic document metadata, but it is a raster PDF: text is no longer selectable or searchable, and interactive PDF features are not preserved. If the result is the same size or larger than the source, Inkslim returns a message recommending Optimize PDF instead.

### Mixed uploads and browser preferences

You can select or drop multiple EPUB and PDF files in one batch. Files are queued and processed one at a time. Inkslim captures the applicable settings when each file is added to the queue, so a mixed batch uses the EPUB profile for EPUB files and the PDF profile for PDF files.

Settings and the expanded/collapsed state of the two settings panels are saved in the browser's localStorage. They are not shared between browsers or devices.

**Clear history** removes completed and failed entries, including their download links. It does not cancel a file currently being processed or remove queued jobs.

## How processing works

### EPUB

The web app passes each uploaded EPUB to epub-shrink.sh.

1. The script extracts the EPUB into a temporary working directory.
2. It finds JPEG and PNG images above the configured size threshold.
3. ImageMagick strips image metadata, fits each image inside the requested dimensions without enlarging it, and optionally converts it to grayscale.
4. For JPEGs, the selected quality and progressive encoding are applied. PNG files are recompressed.
5. An individual image is replaced only when the processed image is smaller.
6. The EPUB is repacked with the required mimetype entry first and stored without compression; the remaining files are deflated.
7. The archive is checked before it is kept. The web app returns an error when no smaller EPUB was produced.

The original upload is not modified.

### PDF — Optimize PDF

The optimizer uses pikepdf to inspect and rewrite the PDF and Pillow to resize/re-encode supported embedded images.

1. Inkslim validates the PDF, checks for a non-empty page tree and reads basic document metadata.
2. It calculates a target image-pixel ceiling from the largest page CropBox and selected DPI.
3. It iterates indirect PDF image objects, skipping unsupported or unsuitable image types.
4. Supported images above the size threshold are decoded, resized if necessary, optionally converted to grayscale, and encoded as JPEG.
5. An image stream is replaced only when the new JPEG is at least 10% smaller.
6. Pikepdf rewrites the PDF with stream compression and object streams.
7. Inkslim reopens the output and verifies that the PDF is readable and the page count is unchanged.

Text and vector drawing commands remain PDF content rather than being rendered into page images. This mode does not run OCR.

### PDF — E-reader mode

E-reader mode uses PDFium via pypdfium2 to render pages and ReportLab to assemble the output.

1. Inkslim validates the source and reads basic document metadata.
2. PDFium renders each page to fit the selected maximum pixel dimensions.
3. Each rendered page is encoded as JPEG, optionally in grayscale.
4. ReportLab creates a new PDF with one full-page image per original page.
5. Inkslim verifies that the output is readable and has the same number of pages.
6. An output that is not smaller than the input is rejected.

## PDF-to-EPUB conversion

PDF-to-EPUB is being developed as a separate asynchronous pipeline. It is deliberately separate from `/shrink`; the existing EPUB/PDF optimization workflow is not routed through the new worker.

The current implementation foundation includes:

- A persistent SQLite job queue using schema migrations (`PRAGMA user_version`) and a shared Docker volume.
- Separate `web` and `worker` Compose services using the same image.
- PDF page analysis with durable, atomic per-page analysis checkpoints.
- Classification of pages so that OCR is only attempted where analysis says it is required.
- Native PDF text extraction into the same durable `PageResult` and line-checkpoint format used by OCR pages.
- PDFium page rendering with configurable DPI, pixel-count and dimension limits.
- Tesseract OCR using TSV output, including word positions and confidence values.
- Explicit per-job OCR language selection (English by default, Dutch, French, German and supported bilingual combinations), with per-page dominant-language estimates in the quality report.
- Durable per-page OCR result checkpoints, so completed pages can be skipped after a worker restart.
- A schema-versioned JSON quality report with per-page decisions, OCR confidence, warnings, EPUB output statistics and aggregate content-quality metrics; recognized book text is deliberately excluded.
- OCR line checkpoints with normalized page geometry, confidence and stable top-to-bottom/left-to-right ordering.
- Conservative per-page paragraph reconstruction shared by native PDF text and OCR, with durable paragraph checkpoints.
- A separate dehyphenation checkpoint stage that removes explicit Unicode soft hyphens while preserving ambiguous visible hyphens and compound words.
- A separate advisory stage that records likely visible end-of-line hyphenation candidates without automatically changing recognized text.
- Document-level detection of repeated headers, footers and consistently positioned page numbers, with separate cleaned paragraph checkpoints.
- Conservative heading and chapter-boundary detection with confidence/reason annotations and durable document/page structure checkpoints.
- A single OCR worker, page-level timeouts and cooperative cancellation checks between pages.

The worker now runs the complete backend conversion path: PDF analysis, native-text extraction and sampled OCR validation, Tesseract OCR where required, line and paragraph reconstruction, conservative dehyphenation, repeated page-furniture removal, chapter/heading detection, document-model assembly and EPUB 3 generation. It stores resumable checkpoints and marks a job complete only after the EPUB has passed runtime structural validation and a JSON quality report has been written. The report contains page diagnostics and aggregate quality metrics, not recognized book text.

The conversion workflow is available in its own section of the web interface. Select one or more PDFs, choose the OCR language and monitor each background job. Completed jobs provide both an EPUB download and a quality report. The existing `/shrink` endpoint remains separate and unchanged. The interface uses one shared upload area: choose **Make smaller** for PDF/EPUB optimization or **Convert PDF to EPUB** for reflowable conversion. Completed conversion jobs offer a readable in-page quality report with a print / Save as PDF action; the original JSON report remains available for diagnostics.

### Conversion quality report

Each completed conversion writes `quality-report.json` beside the generated EPUB. The report uses schema version 5 and includes:

- Source PDF and generated EPUB sizes in bytes.
- Image-preservation metrics: pages scanned and containing images, detected and extracted image counts, extraction failures/skips, missing assets, extracted image bytes, and per-page extraction warnings.
- Embedded image count and bytes, alongside extraction counts and warnings. The report distinguishes extracted assets from images actually packaged in the EPUB.
- Reconstructed chapter, paragraph, heading and word counts.
- Counts of pages with reconstructed text, non-empty source pages without reconstructed text, and pages classified as empty.
- Text-page coverage across non-empty source pages.
- Per-page analysis decisions, OCR diagnostics, confidence values and warnings.
- Per-page semantic content candidates (text, scanned text, mixed, cover, illustration, advertisement, empty or unknown), confidence scores, evidence and aggregate page-type counts.

The `content_quality.warnings` array contains non-fatal content warnings. Current codes include `NO_BODY_TEXT_EXTRACTED` when no readable body text was reconstructed, `OCR_PAGES_WITHOUT_TEXT` when pages requiring OCR have no reconstructed text, and `PAGES_WITHOUT_TEXT` when non-empty source pages have no reconstructed text. These warnings do not by themselves fail a conversion; they flag output that may need manual review. The summary warning count includes both page-level warnings and content-quality warning entries.

Image metrics are derived from durable per-page image checkpoints. If a checkpoint or extracted asset is missing, the report flags incomplete detection or missing assets rather than silently treating the page as image-free. Internal asset paths are not exposed.

The report contains aggregate metrics and diagnostics only. It deliberately does not store recognized book text.

### Runtime EPUB validation

Before a conversion job is completed, Inkslim checks the generated EPUB archive: ZIP integrity, the required uncompressed first `mimetype` entry, safe and unique archive paths, the container's package-document reference, OPF manifest resources, spine references, XHTML well-formedness, navigation links and the presence of readable body text. A failed validation removes the invalid generated output and fails the job instead of exposing a broken book.

This is a focused runtime integrity check, not a replacement for the official EPUBCheck conformance suite. CI continues to run EPUBCheck against generated EPUB fixtures.

### Roadmap

**Implemented and covered by tests**

- Persistent SQLite job queue, schema migrations, worker claiming and interrupted-job recovery.
- PDF validation, page analysis, safe rendering and page classification.
- Native PDF text extraction and sampled OCR validation.
- Tesseract TSV OCR for pages that require recognition, with per-job language selection.
- Durable per-page analysis, text, line, paragraph, cleanup and structure checkpoints.
- Per-page and aggregate JSON quality report (schema version 5), including PDF image extraction and EPUB embedding metrics, EPUB output statistics and non-fatal content-quality warnings, without recognized book text.
- Exact duplicate-cover diagnostics under `images.duplicate_cover_detection`: evaluation status (`complete`, `incomplete` or `not_evaluated`), SHA-256 byte-comparison method, selected first-page cover candidate, matching later page/image indexes, and an explicit indication that no images were removed.
- Line reconstruction with conservative two-column reading order and geometry-based paragraph reconstruction.
- Advisory visible line-end hyphenation detection and conservative soft-hyphen cleanup.
- Document-level repeated header/footer and page-number detection.
- Conservative chapter/heading detection and a durable engine-independent document model.
- EPUB 3 generation from conversion jobs, including navigation, chapters and language metadata.
- Runtime EPUB structural validation before a conversion job is marked complete; official EPUBCheck remains part of CI.

**Next implementation milestones**

1. Review exact duplicate-cover diagnostics and consider removal only after real-book validation; semantic cover metadata and advertisement separation are implemented.
2. Benchmark OCR quality and evaluate alternative engines such as PaddleOCR based on measured quality and resource use.
3. Add optional AI-assisted OCR validation as a separate, opt-in capability; local OCR must continue to work without a cloud API key.

Heading detection is intentionally conservative: it currently recognizes explicit multilingual chapter labels, simple numbered headings, known standalone section labels and short all-caps headings. It does not infer typography from PDF font sizes, and it stores annotations separately rather than modifying source text. Extracted images are placed immediately after a source text block (paragraph or heading) only when the image fits in a clear vertical gap between two text blocks on the same source page. Images without a bounded, unambiguous text anchor remain in a labelled Illustrations section. Page classification now estimates text, scanned text, mixed content, cover candidates, illustration candidates and advertisement candidates. Pages without text or embedded image objects remain unknown because vector drawing content is not inspected. Each estimate includes confidence and reasons. Classification does not alter OCR or discard images. A high-confidence first-page cover candidate is marked using EPUB cover metadata; advertisement-candidate images are kept in a separate Promotional material section. Low-confidence candidates retain the existing safe fallback. Exact duplicate-cover detection is report-only: it compares extracted image bytes with SHA-256 against a qualifying first-page cover and reports byte-identical later-page images. It does not remove images; visual near-duplicate matching and removal are not implemented.

### Persistent data

The Compose file mounts the named Docker volume `conversion-data` at `/data/conversion` in both services. It contains the SQLite database and per-job working directories/checkpoints. Do not remove this volume when recreating containers if you want to preserve conversion jobs. Back up the volume using a consistent stopped-container or SQLite-aware backup procedure.\n\nCompleted, failed and cancelled jobs are automatically removed after `JOB_RETENTION_HOURS` (default 24 hours, measured from completion). Queued and active jobs are never eligible for retention cleanup. `MAX_CONVERSION_STORAGE_MB` (default 5120 MB) is an admission quota for persisted job workspaces: new jobs are rejected with HTTP 507 when current stored workspace usage exceeds the limit. It covers job inputs, checkpoints and outputs, but excludes the SQLite database and temporary upload validation files. This is an admission check, not a hard filesystem quota on additional checkpoint growth during an already-running conversion.

The SQLite database is intended for a local filesystem-backed Docker volume. Do not place it on an NFS or SMB share.

## Command-line EPUB tool

The web application uses epub-shrink.sh for EPUB processing. The script can also be run directly on Linux. It does **not** process PDF files.

### Requirements

- Bash
- ImageMagick 6 (convert) or ImageMagick 7 (magick)
- zip, unzip, stat and numfmt (the latter is optional for human-readable size output)

On Debian/Ubuntu:

    sudo apt-get update
    sudo apt-get install imagemagick zip unzip coreutils

### Examples

    # Show what would be processed without writing an output file
    ./epub-shrink.sh -n book.epub

    # Create a grayscale version next to the original
    ./epub-shrink.sh -g book.epub

    # Process several EPUBs into a separate output directory
    ./epub-shrink.sh -g -o ~/small ~/Books/*.epub

### CLI options

| Option | Description | Default |
|---|---|---:|
| -w WIDTH | Maximum image width in pixels | 768 |
| -h HEIGHT | Maximum image height in pixels | 1024 |
| -q QUALITY | JPEG quality, 1–100 | 75 |
| -m KB | Only process images larger than this size | 50 |
| -g | Convert supported images to grayscale | Off |
| -o DIR | Output directory | Next to the source EPUB |
| -s SUFFIX | Output filename suffix | -small |
| -n | Dry run; show what would be processed | Off |
| -? | Show command help | — |

The CLI exits with a non-zero status for invalid input, missing files, no size reduction, or failed output verification. See the script header for the exit-code definitions.

## HTTP API

The web application exposes a small API:

| Method | Path | Description |
|---|---|---|
| GET | / | Web interface. |
| GET | /healthz | Health check; returns ok. |
| GET | /favicon.ico | Browser favicon. |
| POST | /shrink | Process one uploaded EPUB or PDF. |
| POST | /api/conversions | Upload a PDF and create an asynchronous PDF-to-EPUB job. |
| GET | /api/conversions/{id} | Read conversion status and page progress. |
| GET | /api/conversions | List recent conversion jobs (default 20, maximum 100). |
| DELETE | /api/conversions/history | Permanently clear completed, failed and cancelled jobs and their files; active jobs are retained. |
| DELETE | /api/conversions/{id}/history | Permanently delete one terminal job and its files; active jobs return **409**. |
| DELETE | /api/conversions/{id} | Cancel a queued or active conversion. |
| GET | /api/conversions/{id}/download | Download the validated EPUB after completion. |
| GET | /api/conversions/{id}/report | Download the JSON quality report after completion. |

### PDF-to-EPUB conversion API

The asynchronous conversion API accepts PDF uploads as multipart/form-data. The required file field is `file`; the optional `language` field defaults to `eng` and accepts `nld`, `eng`, `nld+eng`, `fra`, `deu` or `fra+eng`. Automatic language selection is not supported; choose the OCR language explicitly.

- `POST /api/conversions` returns **201** with a server-generated job ID, initial status, progress fields and links to the status, EPUB and report endpoints. Invalid or password-protected PDFs return **422**; missing files, unsupported extensions and invalid language selections return **400**. A full storage admission quota returns **507** (`STORAGE_QUOTA_EXCEEDED`).
- `GET /api/conversions/{id}` returns the job status, progress, safe error details and endpoint links. Unknown or malformed IDs return **404**.
- `GET /api/conversions` lists recent jobs (20 by default; `limit` is clamped to 1–100), including the original upload name, short job ID and action links. Responses use no-store cache headers.
- `DELETE /api/conversions/{id}` requests cancellation. Queued jobs are cancelled immediately; active jobs stop cooperatively between processing pages. A repeated request for a terminal job returns **409**.
- `DELETE /api/conversions/{id}/history` permanently deletes one completed, failed or cancelled job, including its original PDF, checkpoints, EPUB and report. Queued and active jobs cannot be deleted; cancel them first.
- `DELETE /api/conversions/history` permanently deletes all terminal jobs while retaining queued and active jobs. These deletion operations cannot be undone.
- `GET /api/conversions/{id}/download` and `GET /api/conversions/{id}/report` return **409** until the job is complete. They only serve files located inside that job's output directory; the EPUB is structurally validated again before download.

The API reuses the persistent SQLite queue and separate worker service. Upload size is bounded by `MAX_UPLOAD_MB`; PDF page count is bounded by `MAX_PDF_PAGES`. It does not expose filesystem paths or worker internals. Basic Authentication, when enabled, also protects these endpoints.

### POST /shrink

Send a multipart/form-data request containing a file field. The accepted filename extensions are .epub and .pdf.

Common form fields:

| Field | Description |
|---|---|
| file | The EPUB or PDF upload. |
| width | Maximum width in pixels; used for EPUB and PDF E-reader mode. |
| height | Maximum height in pixels; used for EPUB and PDF E-reader mode. |
| quality | JPEG quality, 10–100 in the web app. |
| min_kb | Minimum image size in kB; used by EPUB and PDF Optimize mode. |
| gray | Set to 1 to enable grayscale; otherwise omit or send 0. |

PDF-specific fields:

| Field | Values / meaning |
|---|---|
| pdf_mode | optimize or ereader; defaults to optimize. |
| pdf_dpi | Target image DPI for Optimize mode; accepted range 72–600, default 150. |

A successful response is the processed file as an attachment. The response includes:

- X-Original-Size: input size in bytes.
- X-New-Size: output size in bytes.

Common error responses include 400 for missing or unsupported uploads, 413 for files above the configured upload limit, 422 for invalid files or when no size reduction is possible, and 500 for processing errors.

## Privacy and security

- **Self-hosted processing:** the application does not send documents to an Inkslim cloud service.
- **Synchronous /shrink processing:** uploads and intermediate outputs for the existing optimization endpoint are kept in a temporary directory and removed when processing finishes or fails.
- **Background conversion jobs:** the separate PDF-to-EPUB worker stores uploaded job inputs, SQLite job records and intermediate checkpoints in the persistent conversion-data Docker volume. This is local to your own deployment, not an Inkslim cloud service. Automatic cleanup removes terminal jobs after the configured retention period.
- **Optional Basic Authentication:** disabled by default. Set both BASIC_AUTH_USER and BASIC_AUTH_PASS to require credentials.
- **Health check:** /healthz is intentionally exempt from Basic Authentication so Docker can check container health.
- **Network exposure:** do not expose the service directly to the public internet without additional protection. Prefer a VPN such as WireGuard or Tailscale. If using a reverse proxy, enable HTTPS and configure its upload-size limit and request timeouts.

HTTP Basic Authentication should be used over HTTPS when credentials or documents cross an untrusted network.

## Limitations

### EPUB

- Only JPEG and PNG images inside EPUB archives are processed.
- EPUB HTML, CSS, fonts, metadata and chapter structure are not rewritten.
- Fixed-layout books, comics and image-heavy children's books may be sensitive to image resizing or grayscale conversion. Check the output before replacing the original.
- DRM-protected EPUBs are not supported.
- The shell script does not convert WebP, AVIF or other image formats.

### PDF

- Password-protected PDFs are not supported; remove the password before uploading.
- Optimize PDF currently skips image objects with /SMask, /Mask, /ImageMask, transparency/alpha, and one-bit bilevel image data. Inline PDF images and unsupported encodings may also remain unchanged.
- The DPI ceiling is derived from the largest page CropBox in the document, not the effective displayed size of each individual image. It is therefore an approximation, not a full page-content-aware preflight.
- Some PDFs may not become smaller. Already optimized or bilevel scans can be left unchanged.
- Rewriting a PDF can invalidate digital signatures and may affect interactive features.
- E-reader mode rasterizes pages. Text is no longer selectable or searchable, and links, bookmarks and interactive controls are not retained as functional PDF elements.
- **PDF-to-EPUB is available in the web interface.** The backend analyzes PDFs, extracts native text, runs OCR where needed, reconstructs document structure and generates a structurally validated EPUB. Extracted source-PDF images are placed inline when a clear geometric text anchor exists; uncertain placements remain in a labelled Illustrations section. Semantic page classification guides conservative image handling: first-page cover candidates require confidence ≥0.75 and image coverage ≥45%, while advertisement candidates require confidence ≥0.65. Qualifying covers are marked in EPUB metadata, and advertisement images are preserved in a separate section. Exact duplicate-cover diagnostics use SHA-256 byte equality and do not remove images; near-duplicate visual matching and any removal behavior remain future work.

### General

- Large files and high page counts can take a long time and use CPU and temporary disk space. Processing speed depends on the host hardware.
- The existing `/shrink` endpoint remains synchronous and independent of the conversion worker.
- The conversion worker is configured for one OCR worker in V1. It uses a local SQLite database in the shared Docker volume; use a local Docker volume or local filesystem, not NFS/SMB, for this database.
- The PDF-to-EPUB workflow embeds extracted source-PDF images inline when a bounded geometric anchor is available and otherwise retains them in an Illustrations section. Semantic page classification guides cover metadata and separates advertisement-candidate images without deleting them. Exact duplicate-cover diagnostics compare original extracted bytes by SHA-256 and report matches without deleting images; near-duplicate matching and removal remain future work. Automatic cleanup of expired terminal jobs is enabled.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The page does not open | Check that the container is running, that the host port is mapped to container port 8080, and that the NAS firewall allows the selected host port. |
| Upload returns 413 | Increase MAX_UPLOAD_MB and, if present, the reverse proxy's request-body limit. |
| Processing takes too long | Check the Gunicorn timeout, SHRINK_TIMEOUT for EPUB processing, and reverse-proxy timeouts. |
| PDF says no reduction was possible | Try a lower DPI, lower JPEG quality, or grayscale. Already compact or unsupported images may remain unchanged. |
| E-reader mode says the output would be larger | Use Optimize PDF for text-based PDFs, or try a different page size/quality for a scan. |
| PDF is rejected | Check whether it is password-protected or damaged. |
| Favicon still shows the old icon | Hard-refresh the page, open it in a private window, or clear the browser's cached site data. Favicons are cached separately from the page. |

Useful Docker commands:

    # Pull the latest published image
    docker compose pull

    # Recreate the container
    docker compose up -d --force-recreate

    # Follow the web application logs
    docker compose logs -f web

    # Follow the background worker logs
    docker compose logs -f worker

    # Show container status
    docker compose ps

## Development and tests

### Requirements

- Python 3.12
- Bash, ImageMagick, zip, unzip, coreutils
- ShellCheck for shell-script linting

On Debian/Ubuntu:

    sudo apt-get update
    sudo apt-get install imagemagick zip unzip coreutils shellcheck

Create a virtual environment and install the development dependencies:

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements-dev.txt

Run the tests and shell linter:

    pytest -q
    shellcheck epub-shrink.sh

To run the application locally:

    gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 2 app:app

Then open http://127.0.0.1:8080/.

The GitHub Actions CI workflow installs the system and Python test dependencies, runs ShellCheck, and executes pytest on pushes to main and pull requests. A separate workflow builds and publishes a multi-platform Docker image to GHCR on pushes to main and version tags matching v*.

### Container build

    docker build -t inkslim:local .
    docker run --rm -p 8125:8080 inkslim:local

The Docker image uses Python 3.12, installs the system tools required by the EPUB script and the Tesseract OCR runtime (English, Dutch, French and German language data), installs the Python dependencies, and runs the application as a non-root user.

## Project structure

    .
    ├── app.py                  # Flask web app, upload handling, PDF processing and UI
    ├── epub-shrink.sh          # EPUB image optimization CLI
    ├── conversion/             # Background job queue, PDF analysis, OCR and EPUB writer
    │   ├── __main__.py         # Worker entry point
    │   ├── storage.py          # SQLite job store and durable checkpoints
    │   ├── quality_report.py   # Per-page and aggregate JSON quality report
    │   ├── lines.py            # Deterministic line reconstruction
    │   ├── paragraphs.py       # Geometry-based paragraph reconstruction
    │   ├── dehyphenation.py    # Conservative soft-hyphen cleanup
    │   ├── hyphenation.py      # Advisory visible line-end hyphen detection
    │   ├── worker.py           # Job claiming, recovery and execution
    │   ├── ocr/                # OCR engine interface and Tesseract TSV backend
    │   ├── pdf/                # Analysis, native text, rendering and OCR pipeline
    │   └── epub/               # Dependency-free EPUB 3 writer
    ├── Dockerfile              # Container build
    ├── docker-compose.yml      # Web + worker deployment (host port 8125 -> container 8080)
    ├── requirements.txt        # Runtime Python dependencies
    ├── requirements-dev.txt    # Test dependencies
    ├── static/
    │   ├── dashboard.css          # Responsive dark dashboard styles
    │   ├── inkslim-favicon.png
    │   ├── inkslim-icon-192.png
    │   ├── inkslim-icon-512.png
    │   └── site.webmanifest
    ├── tests/
    │   ├── test_app.py
    │   └── test_pdf.py
    └── .github/
        └── workflows/
            ├── ci.yml
            └── docker.yml

## License

Inkslim is released under the MIT License.

Third-party libraries and system tools remain subject to their own licenses. See their respective project documentation for details.
