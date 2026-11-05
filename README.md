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

    docker compose logs -f inkslim

The Compose file on your NAS is a local file. Pulling a new image does **not** copy a changed docker-compose.yml from GitHub onto the NAS; update the local Compose file separately when its settings change.

## Configuration

The supplied docker-compose.yml sets the host port mapping to 8125:8080 and configures the following environment variables.

| Variable | Default | Description |
|---|---:|---|
| MAX_UPLOAD_MB | 500 | Maximum size of one uploaded file, in MiB. |
| SHRINK_TIMEOUT | 840 | Timeout in seconds for the EPUB shell-script subprocess. |
| LOG_LEVEL | INFO | Python application log level, for example INFO, WARNING or DEBUG. |
| BASIC_AUTH_USER | empty | Optional HTTP Basic Authentication username. |
| BASIC_AUTH_PASS | empty | Optional HTTP Basic Authentication password. |
| PORT | 8080 | Internal listening port used by the Dockerfile's default command. |

Set both BASIC_AUTH_USER and BASIC_AUTH_PASS to enable login. If either is empty, authentication is disabled.

**Timeout note:** the supplied Compose file overrides the image's default Gunicorn command and sets Gunicorn's request timeout to 300 seconds. SHRINK_TIMEOUT is a separate timeout for the EPUB helper process. If you routinely process large files that take several minutes, keep the Gunicorn timeout and SHRINK_TIMEOUT aligned with the processing time you need.

### Port mapping

Docker port mappings use the format HOST_PORT:CONTAINER_PORT:

- 8125 is the port exposed on your NAS or server.
- 8080 is the port used by Inkslim inside the container.

For example, 8125:8080 makes Inkslim available at http://NAS-IP:8125/. The container's internal port remains 8080.

## Using the web interface

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
- **Temporary files:** web uploads and intermediate outputs are kept in a temporary directory and removed when processing finishes or fails.
- **No persistent book library:** Inkslim does not maintain a server-side history of uploaded documents.
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
- OCR and PDF-to-EPUB conversion are **not implemented**.

### General

- Large files and high page counts can take a long time and use CPU and temporary disk space. Processing speed depends on the host hardware.
- The application processes one queued file at a time in each browser session. Multiple browser sessions can submit jobs concurrently, subject to available workers and host resources.

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

    # Follow application logs
    docker compose logs -f inkslim

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

The Docker image uses Python 3.12, installs the system tools required by the EPUB script, installs the Python dependencies, and runs the application as a non-root user.

## Project structure

    .
    ├── app.py                  # Flask web app, upload handling, PDF processing and UI
    ├── epub-shrink.sh          # EPUB image optimization CLI
    ├── Dockerfile              # Container build
    ├── docker-compose.yml      # GHCR deployment (host port 8125 -> container 8080)
    ├── requirements.txt        # Runtime Python dependencies
    ├── requirements-dev.txt    # Test dependencies
    ├── static/
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
