#!/usr/bin/env python3
"""deb2bundle-gui.py -- Deb2Bundle: graphical front-end for deb2bundle.sh

Fill in the form, press "Build .dxe", and watch the build log. If Tkinter or a
display is not available it falls back to a question-and-answer terminal mode.

    python3 deb2bundle-gui.py              GUI (needs: sudo apt install python3-tk)
    python3 deb2bundle-gui.py --terminal   force the terminal mode

Keep this file next to deb2bundle.sh and deb2bundle-stub.sh.
"""
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "deb2bundle.sh"
CONF_FILE = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
             / "deb2bundle" / "gui.json")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DONE_RE = re.compile(r"^==> Done: (.+?) \(")
DEFAULT_MIRROR = "http://deb.debian.org/debian"
COMPONENTS = ["main", "contrib", "non-free", "non-free-firmware"]
BACKENDS = ["auto", "docker", "podman", "debootstrap"]
RELEASE_SUGGESTIONS = ["trixie", "bookworm", "bullseye", "sid"]

# Filled in by start_gui() once tkinter has been imported successfully.
tk = ttk = filedialog = messagebox = None


# ----------------------------------------------------------------- settings ---
def detect_release():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("VERSION_CODENAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def default_settings():
    return {
        "packages": "", "name": "", "release": detect_release(),
        "backend": "auto", "mirror": DEFAULT_MIRROR, "components": ["main"],
        "arches": "", "desc": "", "icon": "", "launch": "", "from_dir": "",
        "output": "", "recommends": False, "no_upgrade": False,
        "reset_base": False,
    }


# Per-application fields are deliberately NOT remembered between sessions.
REMEMBERED = ["release", "backend", "mirror", "components", "arches",
              "recommends", "no_upgrade"]


def load_settings():
    s = default_settings()
    try:
        saved = json.loads(CONF_FILE.read_text())
        for key in REMEMBERED:
            if key in saved:
                s[key] = saved[key]
    except (OSError, ValueError):
        pass
    return s


def save_settings(v):
    try:
        CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONF_FILE.write_text(json.dumps({k: v[k] for k in REMEMBERED}, indent=2))
    except OSError:
        pass


# ------------------------------------------------------------ argument builder ---
def build_args(v, setup_only=False):
    """Turn form values into deb2bundle.sh arguments. Returns (args, error)."""
    try:
        pkgs = shlex.split(v["packages"])
    except ValueError as e:
        return None, f"Cannot read the package list: {e}"

    from_dir = v["from_dir"].strip()
    if setup_only:
        if from_dir:
            return None, "'Existing .deb folder' cannot be combined with 'Set up base only'."
        pkgs = []
    elif not pkgs:
        return None, "Enter at least one package name or .deb file."

    for p in pkgs:
        if p.endswith(".deb") and not Path(p).expanduser().is_file():
            return None, f"File not found: {p}"

    comps = []
    for c in v["components"]:
        if c not in comps:
            comps.append(c)
    if any(not re.fullmatch(r"[a-z][a-z0-9-]*", c) for c in comps):
        return None, "Component names may only contain lowercase letters, digits and '-'."
    if "main" not in comps:
        return None, "The 'main' component must be selected."

    arches = ",".join(a for a in re.split(r"[,\s]+", v["arches"].strip()) if a)
    for a in arches.split(",") if arches else []:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a):
            return None, f"Invalid architecture name: {a}"

    icon = v["icon"].strip()
    if icon and not Path(icon).expanduser().is_file():
        return None, f"Icon file not found: {icon}"
    if from_dir and not Path(from_dir).expanduser().is_dir():
        return None, f"Folder not found: {from_dir}"
    if "'" in v["name"]:
        return None, "The display name must not contain a single quote."

    args = []

    def add(flag, value):
        value = value.strip()
        if value:
            args.extend([flag, value])

    add("-n", v["name"])
    add("-r", v["release"])
    if v["backend"] != "auto":
        add("-b", v["backend"])
    if v["mirror"].strip() and v["mirror"].strip() != DEFAULT_MIRROR:
        add("-m", v["mirror"])
    if comps != ["main"]:
        add("-c", " ".join(comps))
    add("-a", arches)
    add("-D", " ".join(v["desc"].split()))
    add("-i", str(Path(icon).expanduser()) if icon else "")
    add("-l", v["launch"])
    add("-d", str(Path(from_dir).expanduser()) if from_dir else "")
    add("-o", v["output"])
    if v["recommends"]:
        args.append("-R")
    if v["no_upgrade"]:
        args.append("-U")
    if v["reset_base"]:
        args.append("-X")
    if setup_only:
        args.append("-S")
    args.extend(pkgs)
    return args, None


def display_command(args):
    return "deb2bundle.sh " + " ".join(shlex.quote(a) for a in args)


# ------------------------------------------------------------------- askpass ---
def askpass(prompt):
    """Tiny graphical password prompt used by sudo (SUDO_ASKPASS)."""
    import tkinter
    from tkinter import simpledialog
    root = tkinter.Tk()
    root.withdraw()
    pw = simpledialog.askstring("Administrator password",
                                prompt or "Password:", show="*", parent=root)
    root.destroy()
    if pw is None:
        sys.exit(1)
    sys.stdout.write(pw + "\n")
    sys.exit(0)


def make_askpass_helper():
    """Create a temporary executable that sudo can use as its askpass program."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    d = tempfile.mkdtemp(prefix="deb2bundle-askpass-", dir=base if base and os.path.isdir(base) else None)
    script = Path(d) / "askpass.sh"
    script.write_text("#!/bin/sh\nexec %s %s --askpass \"$@\"\n" % (
        shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve()))))
    script.chmod(0o700)
    return d, str(script)


# ------------------------------------------------------------------ terminal UI ---
def _read(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)


def ask(prompt, default=""):
    s = _read(f"{prompt}{f' [{default}]' if default else ''}: ")
    return s or default


def ask_yn(prompt, default=False):
    s = _read(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").lower()
    return default if not s else s.startswith("y")


def run_terminal():
    s = load_settings()
    v = dict(s)
    print("Deb2Bundle (terminal mode)\n")

    setup_only = ask_yn("Only create/refresh the cached base system (no package)?", False)
    if not setup_only:
        v["packages"] = ask("Package names and/or .deb paths (space-separated)")
        v["name"] = ask("Display name (blank = first package)")
    v["release"] = ask("Debian release", s["release"])
    while True:
        v["backend"] = ask(f"Download using ({'/'.join(BACKENDS)})", s["backend"])
        if v["backend"] in BACKENDS:
            break
        print("  please choose one of:", ", ".join(BACKENDS))
    extra = ask("Extra components (contrib non-free non-free-firmware; blank for none)",
                " ".join(c for c in s["components"] if c != "main"))
    v["components"] = ["main"] + extra.replace(",", " ").split()
    v["arches"] = ask("Foreign architectures, e.g. i386 (blank for none)", s["arches"])
    if not setup_only:
        v["desc"] = ask("Description for the welcome screen (blank = from package)")
        v["icon"] = ask("Icon file, PNG (blank for none)")
        v["launch"] = ask("Launch command (blank = package's menu entry)")
        v["recommends"] = ask_yn("Also bundle recommended packages (bigger)?", s["recommends"])
    v["no_upgrade"] = not ask_yn("Upgrade the cached base system first?", not s["no_upgrade"])
    if ask_yn("Show advanced options?", False):
        v["mirror"] = ask("Mirror", s["mirror"])
        if not setup_only:
            v["from_dir"] = ask("Use existing .deb folder instead of downloading (blank = no)")
            v["output"] = ask("Output file (blank = automatic name)")
        v["reset_base"] = ask_yn("Rebuild the cached base system from scratch?", False)

    args, err = build_args(v, setup_only)
    if err:
        print(f"\nError: {err}", file=sys.stderr)
        return 2
    print("\nCommand:\n  " + display_command(args) + "\n")
    if not ask_yn("Run it now?", True):
        return 0
    save_settings(v)
    return subprocess.call(["bash", str(BUILDER), *args])


# ----------------------------------------------------------------------- GUI ---
class Tip:
    """Minimal hover tooltip."""

    def __init__(self, widget, text):
        self.w, self.text, self.tw = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None):
        if self.tw or not self.text:
            return
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tw, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=380,
                 padx=6, pady=3).pack()

    def hide(self, _event=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.output_path = None
        self.askpass_dir = None

        root.title("Deb2Bundle")
        # Fit the screen (e.g. 1366x768 laptops); the form below scrolls if needed.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(840, sw - 40), min(780, sh - 90)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        root.minsize(min(700, w), min(420, h))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

        s = load_settings()
        self.sv = {k: tk.StringVar(value=s[k]) for k in
                   ["packages", "name", "release", "backend", "mirror", "arches",
                    "desc", "icon", "launch", "from_dir", "output"]}
        self.bv = {k: tk.BooleanVar(value=s[k]) for k in
                   ["recommends", "no_upgrade", "reset_base"]}
        self.comp = {c: tk.BooleanVar(value=c in s["components"]) for c in COMPONENTS}

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)     # scrollable form
        main.rowconfigure(2, weight=1)     # log

        # The form lives in a scrollable canvas so every option is reachable
        # on small screens.
        holder = ttk.Frame(main)
        holder.grid(row=0, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        canvas_opts = {"highlightthickness": 0, "borderwidth": 0}
        bg = ttk.Style().lookup("TFrame", "background")
        if bg:
            canvas_opts["background"] = bg
        self.canvas = tk.Canvas(holder, **canvas_opts)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        form = ttk.Frame(self.canvas)
        form.columnconfigure(0, weight=1)
        win = self.canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>",
                  lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(win, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            root.bind_all(seq, self.on_wheel, add="+")

        # -- what to package
        f = self.section(form, 0, "What do you want to package?")
        self.entry(f, 0, "Packages / .deb files", "packages",
                   "Debian package names and/or paths to .deb files, separated by spaces.\n"
                   "Dependencies are downloaded automatically.",
                   ("Add .deb files...", self.browse_debs))
        self.entry(f, 1, "Display name (optional)", "name",
                   "Title shown in the installer. Default: the first package name.")

        # -- target system
        f = self.section(form, 1, "Target system")
        ttk.Label(f, text="Debian release").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        names = [detect_release()] + RELEASE_SUGGESTIONS
        rel = ttk.Combobox(f, textvariable=self.sv["release"],
                           values=[n for i, n in enumerate(names) if n and n not in names[:i]])
        rel.grid(row=0, column=1, sticky="ew", pady=3)
        Tip(rel, "Codename of the Debian release the installer is for, e.g. bookworm.\n"
                 "An installer only works on the release it was built for.")
        ttk.Label(f, text="Download using").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        be = ttk.Combobox(f, textvariable=self.sv["backend"], values=BACKENDS, state="readonly")
        be.grid(row=1, column=1, sticky="ew", pady=3)
        Tip(be, "How the clean minimal Debian is provided: docker, podman, or debootstrap.\n"
                "'auto' picks the first one installed.")
        ttk.Label(f, text="Components").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        cf = ttk.Frame(f)
        cf.grid(row=2, column=1, sticky="w", pady=3)
        for c in COMPONENTS:
            ttk.Checkbutton(cf, text=c, variable=self.comp[c]).pack(side="left", padx=(0, 12))
        self.entry(f, 3, "Foreign architectures", "arches",
                   "Extra architectures to bundle, comma-separated, e.g. i386 (for Wine).\n"
                   "Only supported on an amd64 build machine.")

        # -- installer appearance
        f = self.section(form, 2, "Installer appearance (all optional)")
        self.entry(f, 0, "Description", "desc",
                   "Text on the welcome screen. Default: taken from the package.")
        self.entry(f, 1, "Icon (PNG)", "icon",
                   "Small PNG (under 512 KB) used as the window icon.",
                   ("Browse...", self.browse_icon))
        self.entry(f, 2, "Launch command", "launch",
                   "Command run by the 'Launch' button on the last screen.\n"
                   "Default: the package's first menu entry.")

        # -- options
        f = self.section(form, 3, "Options")
        r = 0
        for key, text, tip in [
            ("recommends", "Also bundle recommended packages (bigger)",
             "Include 'Recommends' dependencies. Off by default."),
            ("no_upgrade", "Don't upgrade the cached base system",
             "Keep the cached minimal Debian as it is. Package lists are still refreshed."),
            ("reset_base", "Rebuild the cached base system from scratch",
             "Throw away the cached minimal Debian and create a new one (slower)."),
        ]:
            cb = ttk.Checkbutton(f, text=text, variable=self.bv[key])
            cb.grid(row=r, column=0, columnspan=3, sticky="w", pady=1)
            Tip(cb, tip)
            r += 1

        # -- advanced
        f = self.section(form, 4, "Advanced")
        self.entry(f, 0, "Mirror", "mirror", "Debian mirror used for downloads.")
        self.entry(f, 1, "Existing .deb folder", "from_dir",
                   "Skip downloading and bundle the .deb files already in this folder.\n"
                   "Dependencies are NOT resolved in this mode.",
                   ("Browse...", self.browse_dir))
        self.entry(f, 2, "Output file", "output",
                   f"Where to save the .dxe file.\nDefault: an automatic name in {os.getcwd()}",
                   ("Browse...", self.browse_output))

        # -- buttons + progress
        bar = ttk.Frame(main)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.btn_setup = ttk.Button(bar, text="Set up base only", command=lambda: self.start(True))
        self.btn_setup.pack(side="left")
        Tip(self.btn_setup, "Create or update the cached minimal Debian without building anything.")
        self.btn_close = ttk.Button(bar, text="Close", command=self.on_close)
        self.btn_close.pack(side="right")
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right", padx=6)
        self.btn_show = ttk.Button(bar, text="Show file", command=self.show_file, state="disabled")
        self.btn_show.pack(side="right", padx=6)
        self.btn_build = ttk.Button(bar, text="Build .dxe", command=lambda: self.start(False))
        self.btn_build.pack(side="right", padx=6)

        # -- log
        lf = ttk.Frame(main)
        lf.grid(row=2, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(1, weight=1)
        top = ttk.Frame(lf)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(top, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(top, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, sticky="e")
        self.log = tk.Text(lf, height=6, wrap="none", state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

    # -- scrolling
    def on_wheel(self, event):
        """Mouse-wheel scrolls the form, except when the pointer is over the log."""
        try:
            under = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if under is None or str(under).startswith(str(self.log)):
            return
        if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0:
            self.canvas.yview_scroll(-3, "units")
        elif getattr(event, "num", 0) == 5 or getattr(event, "delta", 0) < 0:
            self.canvas.yview_scroll(3, "units")

    # -- layout helpers
    def section(self, parent, row, title):
        f = ttk.LabelFrame(parent, text=title, padding=8)
        f.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        f.columnconfigure(1, weight=1)
        return f

    def entry(self, parent, row, label, key, tip, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        e = ttk.Entry(parent, textvariable=self.sv[key])
        e.grid(row=row, column=1, sticky="ew", pady=3)
        Tip(e, tip)
        if browse:
            ttk.Button(parent, text=browse[0], command=browse[1]).grid(
                row=row, column=2, padx=(6, 0), pady=3)
        return e

    # -- file pickers
    def browse_debs(self):
        files = filedialog.askopenfilenames(
            title="Select .deb files",
            filetypes=[("Debian packages", "*.deb"), ("All files", "*")])
        if files:
            cur = self.sv["packages"].get().strip()
            added = " ".join(shlex.quote(f) for f in files)
            self.sv["packages"].set(f"{cur} {added}".strip())

    def browse_icon(self):
        f = filedialog.askopenfilename(title="Select a PNG icon",
                                       filetypes=[("PNG images", "*.png"), ("All files", "*")])
        if f:
            self.sv["icon"].set(f)

    def browse_dir(self):
        d = filedialog.askdirectory(title="Folder containing .deb files")
        if d:
            self.sv["from_dir"].set(d)

    def browse_output(self):
        f = filedialog.asksaveasfilename(title="Save installer as", defaultextension=".dxe",
                                         filetypes=[("Deb2Bundle installers", "*.dxe"), ("All files", "*")])
        if f:
            self.sv["output"].set(f)

    # -- state
    def values(self):
        v = {k: var.get() for k, var in self.sv.items()}
        v.update({k: var.get() for k, var in self.bv.items()})
        v["components"] = [c for c in COMPONENTS if self.comp[c].get()]
        return v

    def set_running(self, running):
        for b in (self.btn_build, self.btn_setup):
            b.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running:
            self.btn_show.configure(state="disabled")
            self.progress.start(12)
        else:
            self.progress.stop()

    def append(self, text):
        text = ANSI_RE.sub("", text)
        for line in text.splitlines():
            m = DONE_RE.match(line.strip())
            if m:
                self.output_path = m.group(1)
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- running the builder
    def start(self, setup_only):
        if self.proc is not None:
            return
        v = self.values()
        args, err = build_args(v, setup_only)
        if err:
            messagebox.showerror("Cannot start", err)
            return
        save_settings(v)

        self.output_path = None
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.append("$ " + display_command(args) + "\n\n")

        env = dict(os.environ, TERM="dumb")
        try:
            self.askpass_dir, env["SUDO_ASKPASS"] = make_askpass_helper()
        except OSError:
            self.askpass_dir = None
        try:
            self.proc = subprocess.Popen(
                ["bash", str(BUILDER), *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env, start_new_session=True)
        except OSError as e:
            self.cleanup_askpass()
            messagebox.showerror("Cannot start", str(e))
            return

        self.status.set("Setting up the base system..." if setup_only else "Building...")
        self.set_running(True)
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.root.after(100, self.poll)

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(line)
        proc.wait()
        self.q.put(None)

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self.finished()
                    return
                self.append(item)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finished(self):
        rc = self.proc.returncode
        self.proc = None
        self.cleanup_askpass()
        self.set_running(False)
        if rc == 0 and self.output_path:
            self.status.set(f"Done: {self.output_path}")
            self.btn_show.configure(state="normal")
            messagebox.showinfo("Finished", f"The installer was built:\n\n{self.output_path}")
        elif rc == 0:
            self.status.set("Finished.")
            messagebox.showinfo("Finished", "The base system is ready and up to date.")
        elif rc is not None and (rc < 0 or rc in (130, 137, 143)):
            self.status.set("Stopped.")
        else:
            self.status.set(f"Failed (exit code {rc}). See the log below.")
            messagebox.showerror("Build failed", f"deb2bundle.sh exited with code {rc}.\n"
                                                 "See the log for details.")

    def stop(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.append("\n[stopping...]\n")
        self.signal_group(signal.SIGTERM)
        self.root.after(8000, lambda: self.signal_group(signal.SIGKILL))

    def signal_group(self, sig):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    def cleanup_askpass(self):
        if self.askpass_dir:
            shutil.rmtree(self.askpass_dir, ignore_errors=True)
            self.askpass_dir = None

    def show_file(self):
        if not self.output_path:
            return
        folder = os.path.dirname(os.path.abspath(self.output_path))
        try:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            messagebox.showinfo("Location", folder)

    def on_close(self):
        if self.proc is not None:
            if not messagebox.askyesno("Build in progress", "A build is still running. Stop it and quit?"):
                return
            self.signal_group(signal.SIGTERM)
        self.cleanup_askpass()
        self.root.destroy()


def start_gui():
    """Open the window. Returns None on success or a reason string if impossible."""
    global tk, ttk, filedialog, messagebox
    try:
        import tkinter as _tk
        from tkinter import ttk as _ttk, filedialog as _fd, messagebox as _mb
    except ImportError:
        return "python3-tk is not installed (sudo apt install python3-tk)"
    tk, ttk, filedialog, messagebox = _tk, _ttk, _fd, _mb
    try:
        root = tk.Tk()
    except tk.TclError as e:
        return f"cannot open a window ({e})"
    App(root)
    root.mainloop()
    return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--askpass":
        askpass(" ".join(sys.argv[2:]))
    if not BUILDER.is_file():
        sys.exit(f"error: deb2bundle.sh must sit next to {Path(__file__).name}")
    if "--terminal" in sys.argv[1:]:
        sys.exit(run_terminal())
    reason = start_gui()
    if reason:
        print(f"GUI unavailable: {reason}\nFalling back to terminal mode.\n", file=sys.stderr)
        sys.exit(run_terminal())


if __name__ == "__main__":
    main()
