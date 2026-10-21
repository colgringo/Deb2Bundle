# Deb2Bundle

**Turn any Debian package into a single, self-installing file for computers that have no internet connection.**

Deb2Bundle builds `.dxe` files. A `.dxe` (a play on .exe) is a normal executable shell script with every package it needs attached to it. Whoever receives one marks it as executable, runs it, enters their password, and the software installs. They never need a terminal, an internet connection, or any knowledge of `apt`.

## Table of contents

- [What it is for](#what-it-is-for)
- [What is in the box](#what-is-in-the-box)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Building a bundle](#building-a-bundle)
  - [Using the graphical builder](#using-the-graphical-builder)
  - [Using the command line](#using-the-command-line)
  - [All options](#all-options)
  - [Examples](#examples)
- [The cached base system](#the-cached-base-system)
- [Installing a bundle (for end users)](#installing-a-bundle-for-end-users)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [FAQ](#faq)

---

## What it is for

Installing software on a Debian machine without internet access is awkward. `apt` needs to download the program and also every library it depends on, and you cannot know in advance which of those libraries the target machine already has. That depends on the desktop environment and whatever else the user has installed.

Deb2Bundle solves this in three steps:

1. You run it **once, on an internet-connected Debian machine**.
2. It works out which packages a *minimal* Debian install is missing for your program, and downloads exactly those. Because it assumes only a minimal system, the bundle works whatever desktop the target uses.
3. It packs everything into one `.dxe` file. On the target machine, the file installs the program, and `apt` skips anything that machine already has.

Typical uses:

- Distributing an application to air-gapped or restricted machines.
- Preparing a USB stick with ready-to-run installers for a lab, classroom or office.
- Giving non-technical users a double-click-style installer for a package that isn't on their machine.
- Bundling your own `.deb` together with all of its dependencies.

## What is in the box

| File | Purpose |
|---|---|
| `deb2bundle.sh` | The builder. Run on an online Debian machine. |
| `deb2bundle-gui.py` | A graphical front-end for the builder (with a terminal fallback). |
| `deb2bundle-stub.sh` | The installer template that becomes the head of every `.dxe`. |

Keep all three files **in the same folder**.

## Requirements

**On the machine that builds bundles (online):**

- Debian or a Debian-based system such as Ubuntu, with `bash`, `dpkg`, `tar` and `sha256sum` (only tested on Debian but in theory should work).
- **One** of: `docker`, `podman`, or `debootstrap` (`sudo apt install debootstrap`). Deb2Bundle uses it to provide a clean minimal Debian.
- For the graphical builder: `python3` and `python3-tk` (`sudo apt install python3-tk`). Without them the builder switches to a terminal question-and-answer mode.
- Internet access while building.
- Several hundred MB of free disk space for the cached base system (see [below](#the-cached-base-system)).

**On the machines that install bundles (offline):**

- Debian, of the **same release and architecture** the bundle was built for (for example Debian 12 "bookworm" on amd64).
- `apt`, which every Debian system has.
- Optional: `zenity`, which gives the graphical installer. It is included in most Debian desktop installs. Without it the installer runs in the terminal.
- No internet connection is needed.

## Quick start

```bash
# 1. On an online Debian machine, in the Deb2Bundle folder:
python3 deb2bundle-gui.py
```

1. Type the package name (for example `htop`) into **Packages / .deb files**.
2. Set **Debian release** to the release your offline machines run (for example `bookworm`).
3. Click **Build .dxe**.

The first build creates the cached base system, which takes a few minutes. Later builds reuse it. When it finishes you get a file such as `htop-bookworm-amd64.dxe`.

```bash
# 2. Copy the .dxe to the offline machine (USB stick etc.), then there:
chmod +x htop-bookworm-amd64.dxe
./htop-bookworm-amd64.dxe
```

Or, in a file manager, tick **Allow executing file as program** in the file's properties and open it. Click **Install** on the welcome screen and enter the password when asked.

---

## Building a bundle

### Using the graphical builder

```bash
python3 deb2bundle-gui.py
```

The window has a scrollable form on top, buttons in the middle and a live log at the bottom.

| Field | What to enter |
|---|---|
| **Packages / .deb files** | Debian package names, and/or paths to your own `.deb` files, separated by spaces. Use **Add .deb files…** to pick files. Dependencies are found automatically. |
| **Display name** | The title shown in the installer. Defaults to the first package name. |
| **Debian release** | The codename of the release the installer is for (`bookworm`, `trixie`, and so on). Defaults to the build machine's release. |
| **Download using** | `auto` (first of docker, podman, debootstrap that is installed), or choose one. |
| **Components** | Tick the Debian archive sections to use. `main` is required. Add `contrib`, `non-free` or `non-free-firmware` if your package lives there. |
| **Foreign architectures** | Extra architectures to bundle, for example `i386` (see [Wine example](#examples)). |
| **Description** | Text for the installer's welcome screen. If left empty it is taken from the package. |
| **Icon (PNG)** | A PNG under 512 KB shown in the installer dialogs. A larger source image gives a sharper icon. |
| **Launch command** | What the **Launch** button on the final screen runs. If left empty, the package's first menu entry is used. |
| **Also bundle recommended packages** | Includes "Recommends" dependencies. Off by default, because it makes bundles much bigger. |
| **Don't upgrade the cached base system** | See [The cached base system](#the-cached-base-system). |
| **Rebuild the cached base system from scratch** | Throws the cached base away and creates a new one. |
| **Mirror** | The Debian mirror to download from. |
| **Existing .deb folder** | Skip downloading and bundle the `.deb` files already in this folder. Dependencies are **not** resolved in this mode. |
| **Output file** | Where to save the result. Defaults to an automatic name in the folder you started the program from. |

Hover over any field for a short explanation.

**Buttons:**

- **Build .dxe** starts the build. The log shows progress.
- **Set up base only** creates or refreshes the cached base system without building anything.
- **Stop** cancels a running build.
- **Show file** opens the folder containing the finished `.dxe`.

The window remembers your release, backend, mirror, components, architectures and the two base-system tick boxes between sessions. Per-application fields (packages, name, description, icon, launch command) are not remembered.

If the build needs administrator rights (for example debootstrap, or docker without group membership), a password prompt appears.

If Tkinter or a display is unavailable, Deb2Bundle says why and asks the same questions in the terminal. You can force this with `python3 deb2bundle-gui.py --terminal`.

### Using the command line

```bash
./deb2bundle.sh [options] PACKAGE_OR_DEB [PACKAGE_OR_DEB ...]
```

Each argument is either a Debian package name or the path to a local `.deb` file. The result is written to `<name>-<release>-<arch>.dxe` in the current directory, unless you pass `-o`.

```bash
./deb2bundle.sh -h        # built-in help
```

### All options

| Option | Meaning |
|---|---|
| `-n NAME` | Display name shown in the installer (default: first package). |
| `-r RELEASE` | Debian codename to build for, e.g. `bookworm` (default: this machine's). |
| `-o FILE` | Output file (default: `NAME-RELEASE-ARCH.dxe`). |
| `-b BACKEND` | `docker`, `podman`, `debootstrap` or `auto` (default). |
| `-m MIRROR` | Debian mirror (default `http://deb.debian.org/debian`). |
| `-c COMPONENTS` | Space-separated components, always including `main`, e.g. `"main contrib non-free non-free-firmware"` (default `main`). |
| `-R` | Also bundle Recommends. |
| `-d DIR` | Don't download; bundle the `.deb` files already in `DIR`. Dependencies are not resolved. |
| `-a ARCHES` | Foreign architectures, comma-separated, e.g. `i386`. Needs an amd64 build machine. |
| `-D TEXT` | Description for the welcome screen. |
| `-i ICON.png` | Icon for the installer windows (under 512 KB). |
| `-l COMMAND` | Command for the **Launch** button. |
| `-S` | Setup only: create or refresh the cached base system, then exit. |
| `-U` | Don't upgrade the cached base system (package lists are still refreshed). |
| `-X` | Delete the cached base system and recreate it. |
| `-h` | Show help. |

### Examples

```bash
# A package from the Debian repositories
./deb2bundle.sh -r bookworm htop

# Your own .deb, plus everything it depends on
./deb2bundle.sh -r bookworm -n "My Tool" ./mytool_1.2_amd64.deb

# Several packages in one installer, with a custom description and launcher
./deb2bundle.sh -r bookworm -n "Office Pack" \
    -D "Everything needed for the front-desk computers." \
    -l "libreoffice" \
    libreoffice-writer libreoffice-calc

# Something from non-free
./deb2bundle.sh -r bookworm -c "main contrib non-free non-free-firmware" firmware-misc-nonfree

# 32-bit packages on a 64-bit system (Wine)
./deb2bundle.sh -r bookworm -a i386 -n Wine wine wine32 wine64
```

When a bundle includes a foreign architecture, the installer enables it on the target machine (`dpkg --add-architecture`). That is a permanent system setting.

---

## The cached base system

To work out what a minimal Debian lacks, Deb2Bundle needs a minimal Debian to compare against. It keeps one between runs, so it is not downloaded again for every bundle.

- **Docker or podman:** a saved image named `deb2bundle-base:<release>-<arch>`.
- **debootstrap:** a directory in `~/.cache/deb2bundle/`.

On each build it:

1. refreshes the package lists,
2. upgrades the base system to the newest point release and security updates (unless `-U` is given),
3. downloads your packages against that base.

The first build for a release creates the base, which takes a few minutes. After that it is quick. Use `-S` to create or refresh it ahead of time, `-U` to keep the base exactly as it is, and `-X` to start over.

**A note on upgrading the base.** An upgraded base assumes newer libraries than an older target machine may have. Debian stable rarely changes dependency requirements in its updates, so this seldom matters. If a bundle ever fails on an older machine with an unmet-dependency error, nothing is changed on that machine. Either update that machine first, or rebuild with `-U -X` to model an older base.

**Disk space.** Expect roughly a few hundred MB for a debootstrap base, and somewhat less for a docker or podman image. Remove it when you no longer need it:

```bash
rm -rf ~/.cache/deb2bundle
docker rmi deb2bundle-base:bookworm-amd64     # or podman rmi, per release/arch
```

---

## Installing a bundle (for end users)

You have been given a `.dxe` file. To install the software:

1. Copy the file somewhere on your computer, such as your Downloads folder.
2. Make it executable: right-click, choose **Properties**, then **Permissions**, and tick **Allow executing file as program**. Or in a terminal: `chmod +x filename.dxe`.
3. Open it. (Double-click if your file manager offers to run it, or run `./filename.dxe` in a terminal.)
4. Read the welcome screen and click **Install**.
5. Enter your password when asked. Use your own password if you are an administrator. On systems where only the root user can install software, use the root (administrator) password.
6. Wait for the progress bar to finish. The last screen offers **Launch** to start the program straight away, or **Exit**.

If the graphical installer isn't available (no `zenity`), the installer opens a terminal window (or runs in your current terminal) with a text progress bar. Everything else works the same way.

### What the installer checks first

Before asking for a password it checks that:

- the computer has the same CPU architecture as the bundle (for example amd64),
- the computer runs the same Debian release the bundle was built for.

If either does not match it stops with an explanation, and nothing is changed. The installer also verifies a checksum, so a damaged or incomplete copy is rejected before anything is installed.

### Installer options

Run these from a terminal:

```bash
./filename.dxe --help           # list the options
./filename.dxe --terminal       # text-only installer
./filename.dxe --yes            # skip the welcome screen / confirmation
./filename.dxe --info           # show the app, version and bundled packages
./filename.dxe --extract DIR    # unpack the .deb files into DIR without installing
./filename.dxe --force          # skip the Debian release check (use with care)
```

### Where is the log, and how do I uninstall?

- The installation log is `/var/log/<app-name>-install.log` (the app name in lowercase, with dashes). If an installation fails, the error screen has a **Show log** button.
- A bundle installs ordinary Debian packages, so you remove the software the usual way, for example `sudo apt remove packagename`. Libraries that came along as dependencies can then be removed with `sudo apt autoremove`.

---

## How it works

1. **Build.** The builder starts a clean minimal Debian (from the cached base). `apt` is told to download your packages without installing them. Whatever it has to download is, by definition, what a minimal Debian is missing.
2. **Index.** The downloaded `.deb` files and a package index are tar-ed into a small local APT repository.
3. **Assemble.** The repository is appended to the installer script, together with its description, version, size, icon and a SHA-256 checksum. The result is the `.dxe` file.
4. **Install.** On the target, the script verifies the checksum, unpacks the repository to a temporary folder, and runs `apt-get install` against **only** that repository. `apt` resolves dependencies, skips what is already installed and never downgrades anything. Progress comes from `apt`'s own status output. The temporary folder is deleted afterwards.

Because it is just a shell script followed by a tar archive, a `.dxe` can be inspected with `--info` and `--extract` at any time.

---

## Limitations

- **One release and architecture per bundle.** A bookworm/amd64 bundle only installs on bookworm/amd64. Build one `.dxe` per release you support. Cross-architecture builds (for example building arm64 bundles on amd64) are not supported.
- **Debian only.** Derivatives such as Ubuntu or Linux Mint are refused by the release check, because their packages differ. (`--force` overrides this, at your own risk.) Ubuntu hosts should also work in theory for *building* bundles for Debian targets.
- **Third-party repositories.** Packages that come from a vendor's own repository can't be downloaded automatically. Download the vendor's `.deb` yourself and pass it as a local file. Its dependencies are then resolved.
- **A snapshot in time.** A bundle contains the package versions available on the day it was built.
- **Packages already on a minimal Debian** can't be bundled on their own. The builder stops with a message saying there was nothing to bundle.
- **Bundles can be large.** Because the builder assumes only a minimal base system, a bundle may include more libraries than a particular target machine needs. `apt` skips the ones it already has during installation, but the file itself still carries them.

---

## Troubleshooting

**"The installer was built for Debian 'X' but this system is 'Y'"**
The release must match. Rebuild with `-r Y`, or use the right bundle for that machine.

**"This installer is for amd64 systems; this machine is …"**
The CPU architecture doesn't match. You need a separate bundle built on a machine of that architecture.

**"Nothing to bundle (everything requested is already in a minimal Debian)"**
The package you asked for already ships with a minimal Debian. There is nothing to add.

**Unmet dependencies during installation**
The target is older than the base the bundle was built against. Update the target if you can, or rebuild with `-U -X` for a more conservative bundle. Nothing is installed when this happens. See `/var/log/<app-name>-install.log` for the exact package.

**The build asks for a password, or docker says "permission denied"**
Docker needs administrator rights unless you are in the `docker` group, and debootstrap always needs them. The builder uses `sudo` automatically. In the graphical builder, a password prompt appears. Alternatively add yourself to the `docker` group, or use podman.

**"python3-tk is not installed"**
Run `sudo apt install python3-tk`, or use the terminal mode (`--terminal`) or the command-line builder.

**No graphical installer appears on the target**
`zenity` isn't installed there. The installer falls back to a terminal window. You can also force it with `./file.dxe --terminal`.

**The graphical password prompt doesn't appear or fails on the target**
Run the bundle with `--terminal` and enter the password there.

**The icon looks small**
The installer shows the icon at a size chosen by the toolkit. A PNG with more pixels (for example 256×256) looks sharper, but may not display larger.

**The first build is slow**
The first build for each release has to create the cached base system. Later builds are much faster. Use `-S` to do this ahead of time.

---

## Security notes

- The bundled repository is marked as trusted when installed, so `apt` does not verify package signatures on the target. The embedded checksum protects against corruption or an incomplete copy. It does **not** protect against someone deliberately replacing the file. Hand `.dxe` files out over trusted channels, and consider signing them (for example with a detached GPG signature) if tampering is a concern.
- Only run a `.dxe` from a source you trust. It installs software with administrator rights, like any installer.
- Build bundles on a machine you trust, from mirrors you trust.

---

## FAQ

**Is a `.dxe` different from a `.run` file?**
No. It is the same kind of file, a shell script with a payload attached. Only the file extension is different. You can pass `-o name.run` if you prefer that name.

**Can I use my own `.deb` files?**
Yes. Pass their paths on the command line or add them in the graphical builder. The builder also resolves their dependencies.

**Can one bundle install several programs?**
Yes. List several packages when building. They install together, and the first one provides the name and description.

**Does the installer change the target's package sources?**
No. It uses its own temporary, isolated APT configuration that points only at the bundled packages, and it leaves the system's `sources.list` alone. The one lasting change is enabling an extra architecture, if you built with `-a`.

**What happens if something is already installed?**
`apt` skips it. If the bundle contains a newer version that the program requires, it upgrades that package. It never downgrades.
