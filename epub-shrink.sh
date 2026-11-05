#!/usr/bin/env bash
#
# epub-shrink.sh - shrink the images inside one or more EPUB files
#
# Based on the idea behind rinze/reduce_ebook_size (unzip, shrink the images,
# zip again), with these improvements:
#   - never enlarges images (only "WxH>"), and keeps an image only when the
#     result is really smaller than the original
#   - case-insensitive (.jpg .JPG .jpeg .png .PNG)
#   - repacks the EPUB according to the standard: 'mimetype' first and stored
#     uncompressed, everything else deflated at maximum compression
#   - handles several EPUBs in one run (including names with spaces)
#   - optional grayscale (the Kobo Glo has a black-and-white screen)
#   - verifies the result before keeping it
#   - the original is NEVER touched or overwritten
#
# Defaults are chosen for a Kobo Glo (768x1024 screen).
#
# Usage:
#   epub-shrink.sh [options] book1.epub [book2.epub ...]
#
# Options:
#   -w WIDTH      maximum width in pixels         (default 768)
#   -h HEIGHT     maximum height in pixels        (default 1024)
#   -q QUALITY    JPEG quality 1-100              (default 75)
#   -m KB         only touch images larger than this (default 50)
#   -g            grayscale (smaller; fine for e-ink without colour)
#   -o DIR        write results to this folder    (default: next to the original)
#   -s SUFFIX     suffix added to the file name   (default "-small")
#   -n            dry run: only show what would happen
#   -?            this help
#
# Exit codes:
#   0  at least one file was written (or a dry run finished)
#   1  usage error or a required program is missing
#   2  no gain: nothing was saved because the result was not smaller
#   3  not a valid EPUB
#   4  the new file failed verification, nothing was saved
#   5  input file not found
# When several files are given, the exit code of the last problem is used,
# unless at least one file was written.
#
# Requires: imagemagick, zip, unzip   (sudo apt install imagemagick zip unzip)

set -euo pipefail

MAX_W=768
MAX_H=1024
QUALITY=75
MIN_KB=50
GRAY=0
OUTDIR=""
SUFFIX="-small"
DRY=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while getopts "w:h:q:m:go:s:n?" opt; do
    case "$opt" in
        w) MAX_W="$OPTARG" ;;
        h) MAX_H="$OPTARG" ;;
        q) QUALITY="$OPTARG" ;;
        m) MIN_KB="$OPTARG" ;;
        g) GRAY=1 ;;
        o) OUTDIR="$OPTARG" ;;
        s) SUFFIX="$OPTARG" ;;
        n) DRY=1 ;;
        *) usage; exit 0 ;;
    esac
done
shift $((OPTIND - 1))

if [[ $# -eq 0 ]]; then
    usage
    exit 1
fi

# Check that the required programs are installed.
for cmd in zip unzip stat; do
    command -v "$cmd" >/dev/null || { echo "Missing: $cmd" >&2; exit 1; }
done
# ImageMagick 7 is called 'magick', ImageMagick 6 is called 'convert'.
if command -v magick >/dev/null; then
    IM=(magick)
elif command -v convert >/dev/null; then
    IM=(convert)
else
    echo "Missing: imagemagick (sudo apt install imagemagick)" >&2
    exit 1
fi

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "$1 B"; }

shrink_image() {
    # Shrink one image; keep the result only if it is smaller.
    local f="$1" ext tmp before after
    ext="${f##*.}"
    ext="${ext,,}"
    tmp="${f%.*}.__tmp.${ext}"
    before=$(stat -c%s "$f")

    local args=(-strip -resize "${MAX_W}x${MAX_H}>")
    [[ $GRAY -eq 1 ]] && args+=(-colorspace Gray)

    case "$ext" in
        jpg|jpeg)
            args+=(-quality "$QUALITY" -interlace Plane -sampling-factor 4:2:0)
            ;;
        png)
            args+=(-depth 8 -define png:compression-level=9)
            ;;
    esac

    if ! "${IM[@]}" "$f" "${args[@]}" "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 0   # unreadable or unusual file: leave it alone
    fi

    after=$(stat -c%s "$tmp")
    if (( after < before )); then
        mv -f "$tmp" "$f"
    else
        rm -f "$tmp"
    fi
}

total_in=0
total_out=0
saved_any=0
problem=0   # exit code of the last problem, see the header

for input in "$@"; do
    if [[ ! -f "$input" ]]; then
        echo "Skipped (not found): $input" >&2
        problem=5
        continue
    fi

    src="$(realpath "$input")"
    name="$(basename "$src")"
    base="${name%.*}"
    dest_dir="${OUTDIR:-$(dirname "$src")}"
    mkdir -p "$dest_dir"
    dest="$(realpath "$dest_dir")/${base}${SUFFIX}.epub"

    if [[ "$dest" == "$src" ]]; then
        echo "Skipped (would overwrite the original): $name" >&2
        problem=1
        continue
    fi

    work="$(mktemp -d)"
    cleanup() { rm -rf "$work"; }
    trap cleanup EXIT

    echo "== $name"
    if ! unzip -q "$src" -d "$work" 2>/dev/null || [[ ! -f "$work/mimetype" ]]; then
        echo "   Not a valid EPUB. Skipped." >&2
        problem=3
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    count=0
    while IFS= read -r -d '' img; do
        count=$((count + 1))
        if [[ $DRY -eq 1 ]]; then
            echo "   would shrink: ${img#"$work"/}"
        else
            shrink_image "$img"
        fi
    done < <(find "$work" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) \
                  -size +"${MIN_KB}"k -print0)

    if [[ $DRY -eq 1 ]]; then
        echo "   ${count} image(s) larger than ${MIN_KB} kB found"
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    # Repack according to the EPUB rules.
    tmp_out="$(mktemp -u "${dest}.XXXXXX")"
    ( cd "$work" \
        && zip -X0 -q "$tmp_out" mimetype \
        && zip -Xr9D -q "$tmp_out" . -x mimetype )

    # Verify the result.
    first="$(unzip -Z1 "$tmp_out" | sed -n 1p)"
    if ! unzip -tq "$tmp_out" >/dev/null 2>&1 || [[ "$first" != "mimetype" ]]; then
        echo "   FAILED: the new file did not pass verification, nothing saved." >&2
        problem=4
        rm -f "$tmp_out"
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    size_in=$(stat -c%s "$src")
    size_out=$(stat -c%s "$tmp_out")

    if (( size_out >= size_in )); then
        echo "   No gain ($(human "$size_in") -> $(human "$size_out")), nothing saved."
        problem=2
        rm -f "$tmp_out"
    else
        mv -f "$tmp_out" "$dest"
        pct=$(( 100 - (size_out * 100 / size_in) ))
        echo "   $(human "$size_in") -> $(human "$size_out")  (-${pct}%)  => $dest"
        total_in=$((total_in + size_in))
        total_out=$((total_out + size_out))
        saved_any=1
    fi

    rm -rf "$work"
    trap - EXIT
done

if (( total_in > 0 )); then
    echo
    echo "Total: $(human "$total_in") -> $(human "$total_out")"
fi

if [[ $DRY -eq 1 || $saved_any -eq 1 ]]; then
    exit 0
fi
exit "$problem"
