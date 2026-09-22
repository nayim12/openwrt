#!/usr/bin/env python3
"""
Debian OpenWrt SD Card Manager
Run on Debian inside VMware.

Recommended:
    sudo python3 openwrt_sd_manager.py

The program is intentionally conservative:
- It detects physical disks and partitions.
- It identifies an OpenWrt/ext4 partition.
- It backs up the partition table and UUID information.
- It runs e2fsck before resizing.
- It extends ONLY partition 2, preserving its starting sector.
- It runs resize2fs after the partition is enlarged.
- It never formats the disk.
"""

import os
import re
import json
import shutil
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

APP = "OpenWrt SD Card Manager"
BACKUP_DIR = Path.home() / "openwrt_sd_backup"


def run_cmd(cmd, timeout=120, input_text=None):
    p = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout
    )
    return p.returncode, p.stdout


def require_root():
    return os.geteuid() == 0


def get_disks():
    rc, out = run_cmd([
        "lsblk", "-J", "-o",
        "NAME,KNAME,PATH,TYPE,SIZE,MODEL,TRAN,RM,RO,FSTYPE,LABEL,PARTUUID,UUID,MOUNTPOINTS"
    ])
    if rc != 0:
        return []
    try:
        return json.loads(out).get("blockdevices", [])
    except Exception:
        return []


def human_disk_text(d):
    name = d.get("path") or d.get("name", "")
    size = d.get("size", "")
    model = (d.get("model") or "").strip()
    tran = d.get("tran") or ""
    rm = d.get("rm")
    return f"{name} | {size} | {model} | {tran} | removable={rm}"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP)
        self.root.geometry("1050x720")
        self.root.minsize(900, 600)

        self.disks = []
        self.selected_disk = None

        self.build_ui()
        self.refresh()

    def log(self, text):
        def _write():
            self.logbox.insert("end", text.rstrip() + "\n")
            self.logbox.see("end")
        self.root.after(0, _write)

    def build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(
            top, text="OpenWrt SD Card Manager",
            font=("TkDefaultFont", 16, "bold")
        ).pack(anchor="w")

        ttk.Label(
            top,
            text="Debian / VMware edition — physical Linux disk operations",
        ).pack(anchor="w", pady=(2, 8))

        warn = ttk.Label(
            top,
            text="WARNING: This tool changes partition boundaries. Verify the selected disk before using EXTEND.",
            foreground="red"
        )
        warn.pack(anchor="w")

        disk_frame = ttk.LabelFrame(self.root, text="Physical disks", padding=8)
        disk_frame.pack(fill="both", expand=False, padx=10, pady=8)

        self.disk_list = tk.Listbox(disk_frame, height=7)
        self.disk_list.pack(side="left", fill="both", expand=True)
        self.disk_list.bind("<<ListboxSelect>>", self.on_select)

        sb = ttk.Scrollbar(disk_frame, orient="vertical", command=self.disk_list.yview)
        sb.pack(side="right", fill="y")
        self.disk_list.config(yscrollcommand=sb.set)

        info = ttk.LabelFrame(self.root, text="Selected disk / Partition 2", padding=8)
        info.pack(fill="x", padx=10, pady=5)

        self.info_text = tk.StringVar(value="No disk selected.")
        ttk.Label(
            info, textvariable=self.info_text,
            font=("TkDefaultFont", 10)
        ).pack(anchor="w")

        buttons = ttk.Frame(self.root, padding=10)
        buttons.pack(fill="x")

        self.btn_refresh = ttk.Button(buttons, text="Refresh Disks", command=self.refresh)
        self.btn_refresh.grid(row=0, column=0, padx=4, pady=4)

        self.btn_detail = ttk.Button(buttons, text="Inspect Selected Disk", command=self.inspect)
        self.btn_detail.grid(row=0, column=1, padx=4, pady=4)

        self.btn_backup = ttk.Button(buttons, text="Backup Partition Table", command=self.backup)
        self.btn_backup.grid(row=0, column=2, padx=4, pady=4)

        self.btn_fsck = ttk.Button(buttons, text="Check / Repair EXT4", command=self.fsck)
        self.btn_fsck.grid(row=0, column=3, padx=4, pady=4)

        self.btn_extend = ttk.Button(buttons, text="EXTEND PARTITION 2", command=self.extend)
        self.btn_extend.grid(row=0, column=4, padx=4, pady=4)

        self.btn_verify = ttk.Button(buttons, text="Verify", command=self.verify)
        self.btn_verify.grid(row=0, column=5, padx=4, pady=4)

        self.btn_restore = ttk.Button(buttons, text="Restore Backup", command=self.restore)
        self.btn_restore.grid(row=1, column=0, padx=4, pady=4)

        self.btn_clear = ttk.Button(buttons, text="Clear Log", command=lambda: self.logbox.delete("1.0", "end"))
        self.btn_clear.grid(row=1, column=1, padx=4, pady=4)

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.logbox = scrolledtext.ScrolledText(
            log_frame, wrap="none", font=("Consolas", 9)
        )
        self.logbox.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x")

    def set_status(self, s):
        self.root.after(0, lambda: self.status.set(s))

    def selected_path(self):
        if not self.selected_disk:
            return None
        return self.selected_disk.get("path")

    def on_select(self, event=None):
        sel = self.disk_list.curselection()
        if not sel:
            return
        self.selected_disk = self.disks[sel[0]]
        self.show_info()

    def show_info(self):
        d = self.selected_disk
        path = d.get("path") or d.get("name")
        children = d.get("children") or []

        p2 = None
        for p in children:
            n = p.get("name", "")
            if n in ("2", path + "2", path + "p2") or n.endswith("2"):
                p2 = p
                break

        if p2:
            text = (
                f"Disk: {path}    Size: {d.get('size','?')}    "
                f"Model: {(d.get('model') or '').strip()}\n"
                f"Partition 2: {p2.get('path') or p2.get('name')}    "
                f"Size: {p2.get('size','?')}    FSTYPE: {p2.get('fstype','?')}    "
                f"UUID: {p2.get('uuid','?')}    PARTUUID: {p2.get('partuuid','?')}"
            )
        else:
            text = f"Disk: {path}    Size: {d.get('size','?')}    No partition 2 detected."
        self.info_text.set(text)

    def refresh(self):
        self.disk_list.delete(0, "end")
        self.disks = get_disks()

        for d in self.disks:
            if d.get("type") == "disk":
                self.disk_list.insert("end", human_disk_text(d))

        self.log("Disks refreshed.")
        self.log("Select the physical SD card. Do NOT select a VMware virtual disk.")

    def inspect(self):
        disk = self.selected_path()
        if not disk:
            messagebox.showwarning(APP, "Select a physical disk first.")
            return

        def worker():
            self.set_status("Inspecting...")
            self.log(f"\n$ lsblk -f {disk}")
            rc, out = run_cmd(["lsblk", "-f", disk])
            self.log(out)

            self.log(f"$ fdisk -l {disk}")
            rc, out = run_cmd(["fdisk", "-l", disk])
            self.log(out)

            self.log(f"$ sfdisk -d {disk}")
            rc, out = run_cmd(["sfdisk", "-d", disk])
            self.log(out)

            self.set_status("Ready")

        threading.Thread(target=worker, daemon=True).start()

    def backup(self):
        disk = self.selected_path()
        if not disk:
            messagebox.showwarning(APP, "Select the SD card first.")
            return

        def worker():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            self.set_status("Creating backup...")

            safe = disk.replace("/", "_")
            table = BACKUP_DIR / f"{safe}_partition-table.sfdisk"
            blkid = BACKUP_DIR / f"{safe}_blkid.txt"
            fdisk = BACKUP_DIR / f"{safe}_fdisk.txt"

            rc, out = run_cmd(["sfdisk", "--dump", disk])
            table.write_text(out)

            rc, out = run_cmd(["blkid", disk])
            blkid.write_text(out)

            rc, out = run_cmd(["fdisk", "-l", disk])
            fdisk.write_text(out)

            self.log(f"Backup saved in: {BACKUP_DIR}")
            self.log(out)
            self.set_status("Backup complete")

        threading.Thread(target=worker, daemon=True).start()

    def get_partition2(self):
        disk = self.selected_path()
        if not disk:
            return None

        rc, out = run_cmd([
            "lsblk", "-J", "-o",
            "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,PARTUUID,UUID,MOUNTPOINTS",
            disk
        ])
        if rc != 0:
            return None

        try:
            d = json.loads(out)["blockdevices"][0]
        except Exception:
            return None

        for p in d.get("children") or []:
            if p.get("type") == "part":
                name = p.get("name", "")
                if name.endswith("2"):
                    return p.get("path")
        return None

    def fsck(self):
        part = self.get_partition2()
        if not part:
            messagebox.showwarning(APP, "Partition 2 was not found.")
            return

        if not messagebox.askyesno(
            APP,
            f"Run filesystem check on {part}?\n\n"
            "The partition must NOT be mounted.\n"
            "This can repair filesystem metadata."
        ):
            return

        def worker():
            self.set_status("Running e2fsck...")
            self.log(f"\n$ umount {part}")
            run_cmd(["umount", part])

            self.log(f"$ e2fsck -f -y {part}")
            rc, out = run_cmd(["e2fsck", "-f", "-y", part], timeout=900)
            self.log(out)

            if rc in (0, 1, 2):
                self.log("e2fsck completed. Review the output above.")
            else:
                self.log(f"e2fsck returned code {rc}. DO NOT resize yet.")

            self.set_status("Filesystem check complete")

        threading.Thread(target=worker, daemon=True).start()

    def extend(self):
        disk = self.selected_path()
        part = self.get_partition2()

        if not disk or not part:
            messagebox.showwarning(APP, "Select the SD card with Partition 2.")
            return

        # Safety check: refuse obvious virtual disks.
        d = self.selected_disk
        tran = (d.get("tran") or "").lower()
        model = (d.get("model") or "").lower()

        if tran not in ("usb", "mmc", "") and "sd" not in model and "card" not in model:
            if not messagebox.askyesno(
                APP,
                f"This does not look like a USB/SD physical disk:\n{disk}\n\nContinue?"
            ):
                return

        # Show exact current information immediately before destructive action.
        rc, fdisk_out = run_cmd(["fdisk", "-l", disk])
        self.log("\nFINAL PRE-RESIZE CHECK:")
        self.log(fdisk_out)

        if not messagebox.askyesno(
            APP,
            f"EXTEND PARTITION 2?\n\n"
            f"Disk: {disk}\n"
            f"Partition: {part}\n\n"
            "Partition 2 will be extended to the end of the disk.\n"
            "Partition 1 will not be intentionally changed.\n"
            "The filesystem will NOT be formatted.\n\n"
            "A backup should already exist.\n\n"
            "Continue?"
        ):
            return

        def worker():
            self.set_status("Extending partition...")
            self.log("\n=== EXTEND START ===")

            # Ensure partition is unmounted.
            self.log(f"$ umount {part}")
            run_cmd(["umount", part])

            # Capture original table and partition start.
            rc, before = run_cmd(["sfdisk", "--dump", disk])
            (BACKUP_DIR / f"{disk.replace('/','_')}_before_resize.sfdisk").write_text(before)
            self.log("Original partition table saved.")

            # Record PARTUUID before.
            rc, before_uuid = run_cmd(["blkid", "-o", "export", part])
            self.log("Original partition metadata:")
            self.log(before_uuid)

            # Filesystem must be clean before changing the boundary.
            self.log(f"$ e2fsck -f {part}")
            rc, out = run_cmd(["e2fsck", "-f", part], timeout=900)
            self.log(out)

            if rc != 0:
                self.log("e2fsck did not return cleanly.")
                self.log("STOPPED. Fix the filesystem before resizing.")
                self.set_status("Stopped — filesystem check required")
                return

            # Use GNU parted resizepart. This changes only the end boundary
            # and keeps the existing start sector and partition type.
            self.log(f"$ parted -s {disk} unit % resizepart 2 100%")
            rc, out = run_cmd([
                "parted", "-s", disk, "unit", "%", "resizepart", "2", "100%"
            ])
            self.log(out)

            if rc != 0:
                self.log("Partition resize failed. Filesystem was not enlarged.")
                self.set_status("Partition resize failed")
                return

            # Ask kernel to reread the partition table.
            self.log("$ partprobe")
            run_cmd(["partprobe", disk])
            run_cmd(["udevadm", "settle"])

            # Determine partition path again in case the kernel refreshed it.
            part2 = self.get_partition2() or part

            self.log(f"$ resize2fs {part2}")
            rc, out = run_cmd(["resize2fs", part2], timeout=1800)
            self.log(out)

            if rc != 0:
                self.log("resize2fs FAILED. Do not format the partition.")
                self.set_status("resize2fs failed")
                return

            self.log(f"$ e2fsck -f {part2}")
            rc, out = run_cmd(["e2fsck", "-f", part2], timeout=900)
            self.log(out)

            self.log("\n=== FINAL PARTITION TABLE ===")
            rc, out = run_cmd(["fdisk", "-l", disk])
            self.log(out)

            self.log("\n=== FINAL UUID ===")
            rc, out = run_cmd(["blkid", part2])
            self.log(out)

            self.set_status("Resize complete — verify output")
            messagebox.showinfo(
                APP,
                "Resize operation finished.\n\n"
                "Check the log and verify the final partition size and UUID."
            )

        threading.Thread(target=worker, daemon=True).start()

    def verify(self):
        disk = self.selected_path()
        if not disk:
            messagebox.showwarning(APP, "Select a disk first.")
            return

        def worker():
            self.set_status("Verifying...")
            self.log("\n=== VERIFY ===")

            for cmd in (
                ["lsblk", "-f", disk],
                ["fdisk", "-l", disk],
                ["sfdisk", "-d", disk],
                ["blkid", disk],
            ):
                self.log("$ " + " ".join(cmd))
                rc, out = run_cmd(cmd)
                self.log(out)

            self.set_status("Verification complete")

        threading.Thread(target=worker, daemon=True).start()

    def restore(self):
        disk = self.selected_path()
        if not disk:
            messagebox.showwarning(APP, "Select the disk first.")
            return

        files = list(BACKUP_DIR.glob(f"{disk.replace('/','_')}*.sfdisk"))
        if not files:
            messagebox.showerror(APP, f"No backup found in {BACKUP_DIR}")
            return

        backup = files[0]

        if not messagebox.askyesno(
            APP,
            f"RESTORE PARTITION TABLE?\n\n"
            f"Disk: {disk}\n"
            f"Backup: {backup}\n\n"
            "This overwrites the current partition table.\n"
            "Do this only when you intentionally want to return to the backup layout."
        ):
            return

        def worker():
            self.set_status("Restoring...")
            self.log(f"$ sfdisk {disk} < {backup}")

            data = backup.read_text()
            rc, out = run_cmd(["sfdisk", "--no-reread", disk], input_text=data)
            self.log(out)

            run_cmd(["partprobe", disk])
            run_cmd(["udevadm", "settle"])

            self.log("Restoration command finished.")
            self.set_status("Restore complete — verify disk")

        threading.Thread(target=worker, daemon=True).start()


def relaunch_as_root():
    """Relaunch this script through pkexec so it can be run directly from Thonny."""
    script = str(Path(__file__).resolve())
    python = shutil.which("python3") or "/usr/bin/python3"

    # pkexec starts a graphical authentication dialog on Debian systems
    # with PolicyKit installed.
    try:
        return subprocess.Popen([
            "pkexec", python, script
        ]).wait()
    except FileNotFoundError:
        messagebox.showerror(
            APP,
            "pkexec is not installed.\n\n"
            "Install it with:\n"
            "sudo apt install policykit-1"
        )
        return 1


def main():
    if not require_root():
        # When launched from Thonny, automatically request root privileges.
        raise SystemExit(relaunch_as_root())

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
