#!/usr/bin/env bash
#
# epub-shrink.sh - verklein de afbeeldingen in een of meer EPUB's
#
# Gebaseerd op het idee van rinze/reduce_ebook_size (uitpakken, afbeeldingen
# verkleinen, opnieuw inpakken), met deze verbeteringen:
#   - verkleint nooit op (alleen "WxH>"), en bewaart een afbeelding alleen
#     als het resultaat echt kleiner is dan het origineel
#   - hoofdletterongevoelig (.jpg .JPG .jpeg .png .PNG)
#   - bouwt de epub volgens de standaard opnieuw op: 'mimetype' als eerste
#     bestand en ongecomprimeerd, de rest met maximale compressie
#   - kan meerdere epubs in een keer aan (ook met spaties in de naam)
#   - optioneel grijswaarden (de Kobo Glo heeft een zwart-wit scherm)
#   - controleert het resultaat voordat het bewaard wordt
#   - het origineel wordt NOOIT aangeraakt of overschreven
#
# Standaardwaarden zijn gekozen voor een Kobo Glo (scherm 768x1024).
#
# Gebruik:
#   epub-shrink.sh [opties] boek1.epub [boek2.epub ...]
#
# Opties:
#   -w BREEDTE    maximale breedte in pixels      (standaard 768)
#   -h HOOGTE     maximale hoogte in pixels       (standaard 1024)
#   -q KWALITEIT  jpeg-kwaliteit 1-100            (standaard 75)
#   -m KB         alleen afbeeldingen groter dan dit aanpassen (standaard 50)
#   -g            grijswaarden (kleiner, geschikt voor e-ink zonder kleur)
#   -o MAP        schrijf resultaat naar deze map (standaard: naast origineel)
#   -s SUFFIX     toevoeging aan de bestandsnaam  (standaard "-small")
#   -n            droog oefenen: toon alleen wat er zou gebeuren
#   -?            deze hulp
#
# Vereist: imagemagick, zip, unzip   (sudo apt install imagemagick zip unzip)

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

# Controleer of de nodige programma's er zijn.
for cmd in zip unzip stat; do
    command -v "$cmd" >/dev/null || { echo "Ontbreekt: $cmd" >&2; exit 1; }
done
# ImageMagick 7 heet 'magick', ImageMagick 6 heet 'convert'.
if command -v magick >/dev/null; then
    IM=(magick)
elif command -v convert >/dev/null; then
    IM=(convert)
else
    echo "Ontbreekt: imagemagick (sudo apt install imagemagick)" >&2
    exit 1
fi

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "$1 B"; }

shrink_image() {
    # Verklein een afbeelding; houd het resultaat alleen als het kleiner is.
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
        return 0   # onleesbaar of vreemd bestand: ongemoeid laten
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

for input in "$@"; do
    if [[ ! -f "$input" ]]; then
        echo "Overgeslagen (niet gevonden): $input" >&2
        continue
    fi

    src="$(realpath "$input")"
    name="$(basename "$src")"
    base="${name%.*}"
    dest_dir="${OUTDIR:-$(dirname "$src")}"
    mkdir -p "$dest_dir"
    dest="$(realpath "$dest_dir")/${base}${SUFFIX}.epub"

    if [[ "$dest" == "$src" ]]; then
        echo "Overgeslagen (zou het origineel overschrijven): $name" >&2
        continue
    fi

    work="$(mktemp -d)"
    cleanup() { rm -rf "$work"; }
    trap cleanup EXIT

    echo "== $name"
    unzip -q "$src" -d "$work"

    if [[ ! -f "$work/mimetype" ]]; then
        echo "   Waarschuwing: geen 'mimetype'-bestand, dit lijkt geen geldige epub. Overgeslagen." >&2
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    count=0
    while IFS= read -r -d '' img; do
        count=$((count + 1))
        if [[ $DRY -eq 1 ]]; then
            echo "   zou verkleinen: ${img#"$work"/}"
        else
            shrink_image "$img"
        fi
    done < <(find "$work" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) \
                  -size +"${MIN_KB}"k -print0)

    if [[ $DRY -eq 1 ]]; then
        echo "   ${count} afbeelding(en) groter dan ${MIN_KB} kB gevonden"
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    # Opnieuw inpakken volgens de epub-regels.
    tmp_out="$(mktemp -u "${dest}.XXXXXX")"
    ( cd "$work" \
        && zip -X0 -q "$tmp_out" mimetype \
        && zip -Xr9D -q "$tmp_out" . -x mimetype )

    # Controleer het resultaat.
    first="$(unzip -Z1 "$tmp_out" | sed -n 1p)"
    if ! unzip -tq "$tmp_out" >/dev/null 2>&1 || [[ "$first" != "mimetype" ]]; then
        echo "   MISLUKT: het nieuwe bestand klopt niet, niets bewaard." >&2
        rm -f "$tmp_out"
        rm -rf "$work"
        trap - EXIT
        continue
    fi

    size_in=$(stat -c%s "$src")
    size_out=$(stat -c%s "$tmp_out")

    if (( size_out >= size_in )); then
        echo "   Geen winst ($(human "$size_in") -> $(human "$size_out")), niets bewaard."
        rm -f "$tmp_out"
    else
        mv -f "$tmp_out" "$dest"
        pct=$(( 100 - (size_out * 100 / size_in) ))
        echo "   $(human "$size_in") -> $(human "$size_out")  (-${pct}%)  => $dest"
        total_in=$((total_in + size_in))
        total_out=$((total_out + size_out))
    fi

    rm -rf "$work"
    trap - EXIT
done

if (( total_in > 0 )); then
    echo
    echo "Totaal: $(human "$total_in") -> $(human "$total_out")"
fi
