#!/bin/bash
# deb2bundle.sh -- build a self-installing .dxe bundle for OFFLINE Debian machines.
#
# Run this on an ONLINE Debian machine. Downloads requested packages and
# dependencies in a minimal Debian environment, builds a local APT repository,
# and appends it to deb2bundle-stub.sh to create one executable .dxe file.
#
# Usage:
#   deb2bundle.sh [options] PACKAGE_OR_DEB [PACKAGE_OR_DEB ...]
#
# Options:
#   -n NAME          Display name (default: first package)
#   -r RELEASE       Debian codename (default: host release)
#   -o FILE          Output file
#   -b BACKEND       docker | podman | debootstrap | auto
#   -m MIRROR        Debian mirror
#   -c COMPONENTS    Repository components
#   -R               Also bundle Recommends
#   -d DIR           Use existing .deb files in DIR; no dependency download
#   -a ARCHES        Foreign architectures, comma-separated (e.g. i386)
#   -D TEXT          Description for the welcome screen (default: from package)
#   -i ICON.png      Icon for the installer windows
#   -l COMMAND       Command for the "Launch" button (default: the package's
#                    first .desktop entry)
#   -S               Setup only: create/refresh the cached base system, then exit
#   -U               Do not upgrade the cached base system (still refreshes lists)
#   -X               Throw away the cached base system and recreate it
#   -h               Show help
#
# The "base system" is a minimal Debian kept between runs (a committed docker/podman
# image, or a debootstrap chroot in ~/.cache/deb2bundle). Each build only
# checks it for updates; it is never re-downloaded.
#
# Example for Wine on amd64:
#   ./deb2bundle.sh -r bookworm -a i386 -n Wine wine wine32 wine64
#
# The target installer must enable the configured foreign architectures
# before installing the bundled packages.

export PATH="$PATH:/usr/sbin"

set -euo pipefail

# When started from deb2bundle-gui.py there is no terminal for sudo to prompt on;
# the GUI provides a graphical password helper through SUDO_ASKPASS.
if [[ -n "${SUDO_ASKPASS:-}" ]]; then
    sudo() { command sudo -A "$@"; }
fi
shopt -u patsub_replacement 2>/dev/null || true

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
STUB="$HERE/deb2bundle-stub.sh"

NAME=""
RELEASE=""
OUTPUT=""
BACKEND=auto
FROM_DIR=""
RECOMMENDS=0
FOREIGN_ARCHES=""
DESC_OVERRIDE=""
ICON_FILE=""
LAUNCH_CMD=""
NO_UPGRADE=0
SETUP_ONLY=0
RESET_BASE=0

MIRROR="http://deb.debian.org/debian"
SECURITY_MIRROR="http://security.debian.org/debian-security"
COMPONENTS="main"

die() {
    echo "error: $*" >&2
    exit 1
}

usage() {
    sed -n '/^# deb2bundle.sh/,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while getopts "n:r:o:b:m:c:d:a:D:i:l:SXURh" opt; do
    case "$opt" in
        n) NAME=$OPTARG ;;
        r) RELEASE=$OPTARG ;;
        o) OUTPUT=$OPTARG ;;
        b) BACKEND=$OPTARG ;;
        m) MIRROR=$OPTARG ;;
        c) COMPONENTS=$OPTARG ;;
        d) FROM_DIR=$OPTARG ;;
        a) FOREIGN_ARCHES=$OPTARG ;;
        D) DESC_OVERRIDE=$OPTARG ;;
        i) ICON_FILE=$OPTARG ;;
        l) LAUNCH_CMD=$OPTARG ;;
        S) SETUP_ONLY=1 ;;
        X) RESET_BASE=1 ;;
        U) NO_UPGRADE=1 ;;
        R) RECOMMENDS=1 ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

shift $((OPTIND - 1))
(( $# || SETUP_ONLY )) || usage 1
[[ -f "$STUB" ]] || die "deb2bundle-stub.sh must sit next to this script"

[[ -z "$ICON_FILE" || -f "$ICON_FILE" ]] || die "icon not found: $ICON_FILE"

case "$BACKEND" in
    auto|docker|podman|debootstrap) ;;
    *) die "invalid backend: $BACKEND" ;;
esac

if [[ -z "$RELEASE" ]]; then
    RELEASE=$(. /etc/os-release; echo "${VERSION_CODENAME:-}")
    [[ -n "$RELEASE" ]] || die "cannot detect release; pass -r"
fi

ARCH=$(dpkg --print-architecture)

# Where the cached base system lives.
BASE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/deb2bundle"
BASE_IMAGE="deb2bundle-base:${RELEASE}-${ARCH}"
BASE_ROOT="$BASE_DIR/chroot-${RELEASE}-${ARCH}"
[[ -z "$FROM_DIR" ]] || (( ! SETUP_ONLY )) || die "-S cannot be combined with -d"

# Parse and validate the requested foreign architectures.
IFS=',' read -r -a FOREIGN_ARCH_ARRAY <<< "$FOREIGN_ARCHES"
for a in "${FOREIGN_ARCH_ARRAY[@]}"; do
    [[ -n "$a" ]] || continue
    [[ "$a" =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
        die "invalid architecture name: $a"
    [[ "$a" != "$ARCH" ]] ||
        die "$a is already the native architecture"
done

# Multiarch downloads require a native amd64 build for the Wine/i386 use case.
if [[ -n "$FOREIGN_ARCHES" && "$ARCH" != "amd64" ]]; then
    die "foreign-architecture mode currently requires an amd64 build host"
fi

# Split arguments into package names and local .deb files.
NAMES=()
TARGETS=()
LOCAL_DEBS=()

for item in "$@"; do
    if [[ "$item" == *.deb && -f "$item" ]]; then
        deb=$(readlink -f "$item")
        LOCAL_DEBS+=("$deb")
        NAMES+=("$(dpkg-deb -f "$deb" Package)")
        TARGETS+=("/in/$(basename "$deb")")
    else
        NAMES+=("$item")
        TARGETS+=("$item")
    fi
done

[[ -n "$NAME" ]] || NAME=${NAMES[0]:-}
[[ "$NAME" != *"'"* ]] || die "NAME must not contain a single quote"

APP_ID=$(printf '%s' "$NAME" |
    tr 'A-Z' 'a-z' |
    tr -c 'a-z0-9\n' '-' |
    sed 's/^-*//; s/-*$//')

[[ -n "$OUTPUT" ]] || OUTPUT="./${APP_ID}-${RELEASE}-${ARCH}.dxe"

WORK=$(mktemp -d /var/tmp/deb2bundle-build.XXXXXX)
SUDO=""
(( EUID == 0 )) || SUDO=sudo

cleanup() {
    $SUDO rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/in" "$WORK/out/debs"

# --------------------------------------------------------------------
# Generate the configuration and inner downloader.
# --------------------------------------------------------------------
write_inner() {
    local drop_lists=1
    [[ "$BACKEND" != debootstrap ]] || drop_lists=0

    cat > "$WORK/in/config.env" <<EOF
RELEASE='$RELEASE'
MIRROR='$MIRROR'
SECURITY_MIRROR='$SECURITY_MIRROR'
COMPONENTS='$COMPONENTS'
RECOMMENDS=$RECOMMENDS
BUILD_ARCH='$ARCH'
FOREIGN_ARCHES='$FOREIGN_ARCHES'
BASE_UPGRADE=$(( ! NO_UPGRADE ))
SETUP_ONLY=$SETUP_ONLY
DROP_LISTS=$drop_lists
HOST_UID=$(id -u)
HOST_GID=$(id -g)
EOF

    if (( SETUP_ONLY )); then
        : > "$WORK/in/targets"
    else
        printf '%s\n' "${TARGETS[@]}" > "$WORK/in/targets"
    fi

    for d in "${LOCAL_DEBS[@]}"; do
        cp "$d" "$WORK/in/"
    done

    cat > "$WORK/in/inner.sh" <<'INNER_EOF'
#!/bin/bash
# Runs inside the cached minimal Debian. Steps:
#   1. enable sources (+ foreign architectures)  2. apt-get update
#   3. upgrade the base (so it stays current)    4. download requested packages
# The base itself is never modified by step 4 (download-only), so whatever apt has
# to fetch is exactly what a minimal Debian is missing.
set -euo pipefail
shopt -s nullglob
export DEBIAN_FRONTEND=noninteractive

. /in/config.env

mkdir -p /out
ADDED_ARCHES=()

cleanup() {
    local a
    for a in "${ADDED_ARCHES[@]}"; do
        dpkg --remove-architecture "$a" >/dev/null 2>&1 || true
    done
    apt-get clean >/dev/null 2>&1 || true
    if (( DROP_LISTS )); then rm -rf /var/lib/apt/lists/*; fi
}
trap cleanup EXIT

rm -f /etc/apt/sources.list.d/debian.sources

{
    echo "deb $MIRROR $RELEASE $COMPONENTS"

    case "$RELEASE" in
        sid|unstable)
            ;;
        *)
            echo "deb $MIRROR $RELEASE-updates $COMPONENTS"
            echo "deb $SECURITY_MIRROR $RELEASE-security $COMPONENTS"
            ;;
    esac
} > /etc/apt/sources.list

# Enable foreign architectures before updating package lists.
if [[ -n "$FOREIGN_ARCHES" ]]; then
    IFS=',' read -r -a arches <<< "$FOREIGN_ARCHES"
    for arch in "${arches[@]}"; do
        [[ -n "$arch" ]] || continue
        have_arches=" $(dpkg --print-foreign-architectures | tr '\n' ' ') "
        if [[ "$have_arches" != *" $arch "* ]]; then
            dpkg --add-architecture "$arch"
            ADDED_ARCHES+=("$arch")
        fi
    done
fi

APT=(-y -o APT::Sandbox::User=root
     -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

if (( ! RECOMMENDS )); then
    APT+=(--no-install-recommends)
fi

pkgstate() { dpkg-query -W -f '${binary:Package} ${Version}\n' | sort | md5sum; }

echo "==> Checking the base system for updates"
apt-get update

before=$(pkgstate)
if (( BASE_UPGRADE )); then
    # Never pull Recommends into the base, even for -R builds.
    apt-get "${APT[@]}" --no-install-recommends dist-upgrade
fi
if [[ "$(pkgstate)" != "$before" ]]; then
    touch /out/.base-changed
fi

if (( SETUP_ONLY )); then
    exit 0
fi

# Drop anything the upgrade just downloaded so only this build's packages remain.
apt-get clean

mapfile -t TARGETS < /in/targets

# Resolve and download all requested packages and their dependencies,
# including dependencies for enabled foreign architectures.
apt-get "${APT[@]}" install --download-only "${TARGETS[@]}"

mkdir -p /out/debs
cp /var/cache/apt/archives/*.deb /out/debs/ 2>/dev/null || true
cp /in/*.deb /out/debs/ 2>/dev/null || true

debs=(/out/debs/*.deb)
(( ${#debs[@]} )) || {
    echo "error: nothing to bundle (everything requested is already in a minimal Debian)" >&2
    exit 3
}

chown -R "$HOST_UID:$HOST_GID" /out 2>/dev/null || true
INNER_EOF

    chmod +x "$WORK/in/inner.sh"
}

# --------------------------------------------------------------------
# Select the download backend.
# --------------------------------------------------------------------
pick_backend() {
    if [[ "$BACKEND" == auto ]]; then
        for e in docker podman debootstrap; do
            if command -v "$e" >/dev/null 2>&1; then
                BACKEND=$e
                break
            fi
        done

        [[ "$BACKEND" != auto ]] ||
            die "need docker, podman or debootstrap"
    fi

    command -v "$BACKEND" >/dev/null 2>&1 ||
        die "$BACKEND is not installed"
}

fetch_container() {
    local pre=() image="$BASE_IMAGE" fresh=0 name="deb2bundle-build-$$" old new

    "$BACKEND" info >/dev/null 2>&1 || pre=(sudo)

    if (( RESET_BASE )); then
        "${pre[@]}" "$BACKEND" rmi -f "$BASE_IMAGE" >/dev/null 2>&1 || true
    fi

    if "${pre[@]}" "$BACKEND" image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
        echo "==> Using cached base system ($BASE_IMAGE)"
    else
        echo "==> Creating the base system image ($BASE_IMAGE) -- first run only"
        fresh=1
        image="debian:${RELEASE}-slim"
    fi

    if ! "${pre[@]}" "$BACKEND" run --name "$name" \
            -v "$WORK/in:/in:ro" \
            -v "$WORK/out:/out" \
            "$image" \
            bash /in/inner.sh; then
        "${pre[@]}" "$BACKEND" rm -f "$name" >/dev/null 2>&1 || true
        die "download step failed"
    fi

    # Keep the (possibly upgraded) base for next time.
    if (( fresh )) || [[ -e "$WORK/out/.base-changed" ]]; then
        echo "==> Saving updated base system"
        old=$("${pre[@]}" "$BACKEND" images -q "$BASE_IMAGE" 2>/dev/null || true)
        "${pre[@]}" "$BACKEND" commit "$name" "$BASE_IMAGE" >/dev/null
        new=$("${pre[@]}" "$BACKEND" images -q "$BASE_IMAGE" 2>/dev/null || true)
        if [[ -n "$old" && "$old" != "$new" ]]; then
            "${pre[@]}" "$BACKEND" rmi "$old" >/dev/null 2>&1 || true
        fi
    else
        echo "==> Base system already up to date"
    fi

    "${pre[@]}" "$BACKEND" rm -f "$name" >/dev/null 2>&1 || true
}

fetch_debootstrap() {
    local root="$BASE_ROOT"

    mkdir -p "$BASE_DIR"

    if (( RESET_BASE )) || [[ -d "$root" && ! -e "$root/.base-ok" ]]; then
        $SUDO rm -rf "$root"
    fi

    if [[ -e "$root/.base-ok" ]]; then
        echo "==> Using cached base system ($root)"
    else
        echo "==> Creating the base system in $root -- first run only"
        $SUDO debootstrap --variant=minbase "$RELEASE" "$root" "$MIRROR"
        $SUDO touch "$root/.base-ok"
    fi

    # The network may have changed since the base was created.
    $SUDO cp -L --remove-destination /etc/resolv.conf "$root/etc/resolv.conf"

    $SUDO rm -rf "$root/in" "$root/out"
    $SUDO mkdir -p "$root/in" "$root/out"
    $SUDO cp -r "$WORK/in/." "$root/in/"

    local rc=0
    $SUDO chroot "$root" bash /in/inner.sh || rc=$?

    $SUDO cp -r "$root/out/." "$WORK/out/" || true
    $SUDO chown -R "$(id -u):$(id -g)" "$WORK/out"
    $SUDO rm -rf "$root/in" "$root/out"

    (( rc == 0 )) || die "download step failed"
}

# Build the flat-repository index on the host. (Doing this inside the cached base
# would require installing apt-utils there and pollute the "minimal" system.)
make_index() {
    local f
    (
        cd "$WORK/out"
        for f in debs/*.deb; do
            dpkg-deb -f "$f"
            echo "Filename: $f"
            echo "Size: $(stat -c %s "$f")"
            echo "MD5sum: $(md5sum "$f" | cut -d' ' -f1)"
            echo "SHA256: $(sha256sum "$f" | cut -d' ' -f1)"
            echo
        done
    ) | gzip -9n > "$WORK/out/Packages.gz"
}

# --------------------------------------------------------------------
# Use a directory of existing .deb files without downloading.
# --------------------------------------------------------------------
make_index_locally() {
    mkdir -p "$WORK/out/debs"

    local d
    for d in "$FROM_DIR"/*.deb; do
        [[ -f "$d" ]] && cp "$d" "$WORK/out/debs/"
    done

    for d in "${LOCAL_DEBS[@]}"; do
        cp "$d" "$WORK/out/debs/"
    done

    compgen -G "$WORK/out/debs/*.deb" >/dev/null ||
        die "no .deb files found in $FROM_DIR"

    make_index
}

# --------------------------------------------------------------------
# Fetch packages or use the provided directory.
# --------------------------------------------------------------------
if [[ -n "$FROM_DIR" ]]; then
    [[ -d "$FROM_DIR" ]] || die "$FROM_DIR is not a directory"

    if [[ -n "$FOREIGN_ARCHES" ]]; then
        echo "WARNING: -d mode does not resolve dependencies or verify"
        echo "         that all requested architectures are present."
    fi

    echo "==> Using .deb files from $FROM_DIR"
    make_index_locally
else
    pick_backend

    echo "==> Downloading for Debian $RELEASE/$ARCH using $BACKEND"
    if [[ -n "$FOREIGN_ARCHES" ]]; then
        echo "==> Enabling foreign architectures: $FOREIGN_ARCHES"
    fi

    write_inner

    case "$BACKEND" in
        docker|podman) fetch_container ;;
        debootstrap) fetch_debootstrap ;;
        *) die "unknown backend $BACKEND" ;;
    esac

    if (( SETUP_ONLY )); then
        echo "==> Base system for Debian $RELEASE/$ARCH is ready and up to date."
        exit 0
    fi

    compgen -G "$WORK/out/debs/*.deb" >/dev/null || die "no .deb files were produced"
    make_index
fi

compgen -G "$WORK/out/debs/*.deb" >/dev/null ||
    die "no .deb files were produced"

[[ -f "$WORK/out/Packages.gz" ]] ||
    die "package index was not produced"

# --------------------------------------------------------------------
# Collect metadata for the installer's welcome screen.
# --------------------------------------------------------------------
read_meta() {
    local idx="$WORK/out/Packages.gz" txt="$WORK/Packages.txt" first=${NAMES[0]%%:*} st


    zcat "$idx" > "$txt"

    N_PKGS=$(grep -c '^Package:' "$txt" || true)
    TOTAL_KB=$(awk '/^Installed-Size:/ { s += $2 } END { print s + 0 }' "$txt")

    st=$(awk -v want="$first" '
        BEGIN { RS = ""; FS = "\n" }
        { for (i = 1; i <= NF; i++) if ($i == "Package: " want) { print; exit } }' "$txt")

    APP_VERSION=$(sed -n '/^Version: /{s///;p;q;}'  <<< "$st")
    APP_URL=$(sed -n '/^Homepage: /{s///;p;q;}'     <<< "$st")
    APP_SUMMARY=$(sed -n '/^Description: /{s///;p;q;}' <<< "$st")
    APP_DESC=$(awk '
        /^Description:/ { d = 1; next }
        d && /^ /       { sub(/^ /, ""); print ($0 == "." ? "" : $0); next }
        d               { d = 0 }' <<< "$st")
    APP_DESC=${APP_DESC:0:700}
    [[ -z "$DESC_OVERRIDE" ]] || APP_DESC=$DESC_OVERRIDE

    ICON_B64=""
    if [[ -n "$ICON_FILE" ]]; then
        (( $(stat -c %s "$ICON_FILE") < 524288 )) || die "icon is larger than 512 KB"
        ICON_B64=$(base64 -w0 "$ICON_FILE")
    fi

    META=$(
        printf 'APP_VERSION=%q\n' "$APP_VERSION"
        printf 'APP_SUMMARY=%q\n' "$APP_SUMMARY"
        printf 'APP_DESC=%q\n'    "$APP_DESC"
        printf 'APP_URL=%q\n'     "$APP_URL"
        printf 'N_PKGS=%q\n'      "$N_PKGS"
        printf 'TOTAL_KB=%q\n'    "$TOTAL_KB"
        printf 'LAUNCH_CMD=%q\n'  "$LAUNCH_CMD"
        printf 'ICON_B64=%q\n'    "$ICON_B64"
    )
}

# --------------------------------------------------------------------
# Assemble the self-extracting installer.
# --------------------------------------------------------------------
read_meta

echo "==> Assembling $OUTPUT"

tar --owner=0 --group=0 -cf "$WORK/payload.tar" \
    -C "$WORK/out" debs Packages.gz

SHA256=$(sha256sum "$WORK/payload.tar" | cut -d' ' -f1)

tpl=$(<"$STUB")
tpl=${tpl//@APP_NAME@/$NAME}
tpl=${tpl//@APP_ID@/$APP_ID}
tpl=${tpl//@MAIN_PKGS@/${NAMES[*]}}
tpl=${tpl//@RELEASE@/$RELEASE}
tpl=${tpl//@ARCH@/$ARCH}
tpl=${tpl//@SHA256@/$SHA256}

# The stub can consume this placeholder if updated to enable multiarch.
tpl=${tpl//@FOREIGN_ARCHES@/$FOREIGN_ARCHES}
# Substituted last so description text can never be re-expanded.
tpl=${tpl//@META@/$META}

printf '%s\n' "$tpl" > "$WORK/header.sh"

cat "$WORK/header.sh" "$WORK/payload.tar" > "$OUTPUT"
chmod +x "$OUTPUT"

count=$(find "$WORK/out/debs" -maxdepth 1 -name '*.deb' | wc -l)

echo "==> Done: $OUTPUT ($count packages, $(du -h "$OUTPUT" | cut -f1))"
if [[ -n "$FOREIGN_ARCHES" ]]; then
    echo "==> Foreign architectures bundled: $FOREIGN_ARCHES"
    echo "==> Ensure deb2bundle-stub.sh enables these architectures on target."
fi
