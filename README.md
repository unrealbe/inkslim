# inkslim

Drag an EPUB onto a web page, get a smaller one back. Made for e-ink readers
with small, black-and-white screens (think a 6-inch Kobo Glo), where a 40 MB
picture book is mostly wasted pixels.

<!-- Add a screenshot: take one, save it as docs/screenshot.png, then uncomment:
![inkslim screenshot](docs/screenshot.png)
-->

- **Drag & drop** one or more `.epub` files, download the slimmed versions.
- **Tuned for e-ink**: defaults to 768 x 1024 and grayscale, with a preset for colour screens.
- **Safe by design**: images are only ever shrunk, never enlarged, and an image is only
  replaced when the new version is actually smaller. Your original is never touched.
- **Nothing is stored**: every upload is processed in a temporary folder and deleted.
- **Tiny and readable**: a ~200-line shell script (ImageMagick + zip) and a ~300-line Flask app.
- **Self-hosted**: runs in Docker on a NAS, Raspberry Pi or any Linux box. No account, no cloud.

## Quick start (Docker)

```bash
git clone https://github.com/YOUR-GITHUB-USERNAME/inkslim.git
cd inkslim
docker compose up -d --build
```

Then open <http://localhost:8080> (or `http://YOUR-NAS-IP:8080`).

Or run the prebuilt multi-arch image (amd64 and arm64):

```bash
docker run -d --name inkslim -p 8080:8080 --restart unless-stopped \
  ghcr.io/YOUR-GITHUB-USERNAME/inkslim:latest
```

### Configuration

Environment variables:

| Variable          | Default | Meaning                                                    |
|-------------------|---------|------------------------------------------------------------|
| `MAX_UPLOAD_MB`   | `500`   | Maximum size of one upload                                 |
| `SHRINK_TIMEOUT`  | `840`   | Maximum processing time per book, in seconds               |
| `BASIC_AUTH_USER` | (empty) | Username for HTTP basic auth. Empty means no login         |
| `BASIC_AUTH_PASS` | (empty) | Password for HTTP basic auth                               |

### Security

There is **no login by default**. Keep it on your own network, or set
`BASIC_AUTH_USER` and `BASIC_AUTH_PASS`. To reach it from outside your home,
prefer a VPN (Tailscale, WireGuard) over exposing it to the internet. If you put a
reverse proxy in front, raise its upload limit (`client_max_body_size` in nginx)
and its timeouts, otherwise big books fail with 413 or 504 errors.

## Command line

The web page is a thin wrapper around `epub-shrink.sh`, which works on its own:

```bash
sudo apt install imagemagick zip unzip

./epub-shrink.sh -n book.epub                       # dry run: what would change?
./epub-shrink.sh -g book.epub                       # grayscale, writes book-small.epub
./epub-shrink.sh -g -o ~/small ~/Books/*/*.epub     # a whole library, into another folder
```

| Option        | Meaning                                              | Default |
|---------------|------------------------------------------------------|---------|
| `-w WIDTH`    | Maximum width in pixels                              | 768     |
| `-h HEIGHT`   | Maximum height in pixels                             | 1024    |
| `-q QUALITY`  | JPEG quality, 1-100                                  | 75      |
| `-m KB`       | Only touch images larger than this                   | 50      |
| `-g`          | Convert to grayscale                                 | off     |
| `-o DIR`      | Write results to this folder                         | next to the original |
| `-s SUFFIX`   | Suffix for the output file name                      | `-small` |
| `-n`          | Dry run                                              |         |

## How it works

1. Unzip the EPUB into a temporary folder.
2. For every JPEG and PNG above the size threshold, resize it to fit inside the
   target box (`WxH>`, so smaller images are left alone), strip metadata, optionally
   convert to grayscale, and keep the result only if it is smaller.
3. Repack according to the EPUB rules: `mimetype` first and stored uncompressed,
   everything else deflated.
4. Check that the new file is a valid zip with `mimetype` first, and that it is
   really smaller, before saving it.

File names inside the book never change, so all references in the book keep working.

## Limitations

- Only JPEG and PNG images are processed. No WebP/AVIF conversion, no HTML or CSS changes.
- Fixed-layout books (some children's books and comics) may look different after
  resizing. Always check the result before replacing your original.
- It does not deal with DRM. DRM-protected books cannot be processed.
- A large book can take a few minutes on a slow NAS.
- Grayscale turns colour picture books black and white. That is the point on an
  e-ink screen without colour, but not on a tablet.

## Development

```bash
pip install -r requirements-dev.txt
sudo apt install imagemagick zip unzip shellcheck
pytest
shellcheck epub-shrink.sh
```

CI runs the tests and shellcheck on every push, and publishes a multi-arch Docker
image to GitHub Container Registry for `main` and for version tags (`v1.0.0`).

## Alternatives

This is deliberately a small, simple tool. If you need more, have a look at:

- [ePubLift](https://github.com/ePubLift/epublift): Rust, modernizes EPUB structure and
  re-encodes images, also offers a self-hostable web service.
- [epubkit](https://github.com/b1rdmania/epubkit): web-based EPUB optimizer for e-ink
  readers, with a longer processing pipeline.
- [epuboptim](https://github.com/martinus/epuboptim): CLI that drives tools such as
  `jpegoptim` and `oxipng`.
- [TinyEpub](https://github.com/sadatdaniel/TinyEpub) and
  [epub-shrink](https://github.com/murrple-1/epub-shrink): command line tools that
  resize the images in an EPUB.

## Credits

The idea of "unzip, shrink the images, zip again" comes from
[José María Mateos' blog post](https://rinzewind.org/blog-en/2024/simple-bash-script-to-reduce-the-size-of-an-epub-file.html).
This project is an independent rewrite with a web interface, safety checks and tests.
[ImageMagick](https://imagemagick.org) does the image work.

## License

[MIT](LICENSE)
