"""Post-processing: par2 verify/repair, archive extraction, cleanup and moving to the final folder.
All functions are blocking and are run in a worker thread."""
import logging
import os
import re
import shutil
import subprocess
import zipfile

log = logging.getLogger("postprocess")

RAR_FIRST = re.compile(r"(\.part0*1\.rar|(?<!\.part\d)(?<!\.part\d\d)(?<!\.part\d\d\d)\.rar)$", re.I)
RAR_PART = re.compile(r"\.part\d+\.rar$", re.I)
SEVENZ_FIRST = re.compile(r"\.7z(\.0*1)?$", re.I)
ZIP_FIRST = re.compile(r"\.zip$", re.I)


def have(tool):
    return shutil.which(tool) is not None


def tools():
    return {t: have(t) for t in ("par2", "unrar", "7z", "7zz")}


def _run(cmd, cwd=None, timeout=6 * 3600):
    log.info("run: %s", " ".join(c if not c.startswith("-p") else "-p***" for c in cmd))
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")[-4000:]


def list_files(d):
    out = []
    for root, _, files in os.walk(d):
        for f in files:
            out.append(os.path.join(root, f))
    return out


def par2_files(d):
    return sorted([f for f in list_files(d) if f.lower().endswith(".par2")], key=os.path.getsize)


def par2_repair(d, force_verify=False, log_cb=print):
    """Returns (ok, message). ok=None means nothing to do / not possible."""
    pars = par2_files(d)
    if not pars:
        return None, "No par2 files"
    if not have("par2"):
        return None, "par2 tool not installed"
    main = pars[0]
    others = [f for f in list_files(d) if not f.lower().endswith(".par2") and not f.endswith(".state.json")]
    log_cb("Verifying with par2")
    code, out = _run(["par2", "v", "-q", main] + others, cwd=d)
    if code == 0:
        return True, "Verified OK"
    log_cb("Repairing with par2")
    code, out = _run(["par2", "r", "-q", main] + others, cwd=d)
    if code == 0:
        # par2 keeps damaged originals as "<name>.1"; remove them once repaired
        for f in list_files(d):
            m = re.match(r"(.+)\.[1-9]\d?$", f)
            if m and os.path.exists(m.group(1)):
                os.remove(f)
        return True, "Repaired"
    last = out.strip().splitlines()[-1] if out.strip() else ""
    return False, f"par2 repair failed {last}".strip()


def find_archives(d):
    archives = []
    for f in sorted(list_files(d)):
        name = os.path.basename(f)
        if RAR_PART.search(name):
            if re.search(r"\.part0*1\.rar$", name, re.I):
                archives.append(("rar", f))
        elif name.lower().endswith(".rar"):
            archives.append(("rar", f))
        elif SEVENZ_FIRST.search(name):
            archives.append(("7z", f))
        elif ZIP_FIRST.search(name):
            archives.append(("zip", f))
    return archives


def volumes_of(kind, path):
    d, name = os.path.split(path)
    files = os.listdir(d)
    if kind == "rar":
        if RAR_PART.search(name):
            base = RAR_PART.sub("", name)
            pat = re.compile(re.escape(base) + r"\.part\d+\.rar$", re.I)
        else:
            base = name[:-4]
            pat = re.compile(re.escape(base) + r"\.(rar|r\d{2,3}|s\d{2})$", re.I)
    elif kind == "7z":
        base = re.sub(r"\.7z(\.\d+)?$", "", name, flags=re.I)
        pat = re.compile(re.escape(base) + r"\.7z(\.\d{3})?$", re.I)
    else:
        base = name[:-4]
        pat = re.compile(re.escape(base) + r"\.(zip|z\d{2})$", re.I)
    return [os.path.join(d, f) for f in files if pat.match(f)]


def extract_one(kind, path, dest, password=""):
    os.makedirs(dest, exist_ok=True)
    if kind == "rar" and have("unrar"):
        cmd = ["unrar", "x", "-o+", "-y", "-idq", f"-p{password}" if password else "-p-", path, dest + os.sep]
        return _run(cmd)
    sevenz = "7zz" if have("7zz") else ("7z" if have("7z") else None)
    if sevenz:
        return _run([sevenz, "x", "-y", f"-p{password}", f"-o{dest}", path])
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(dest, pwd=password.encode() if password else None)
            return 0, ""
        except Exception as e:
            return 1, str(e)
    return 1, f"No extractor available for {kind}"


def extract_all(d, password="", nested=True, delete_archives=True, log_cb=print):
    """Extract every archive in d (in place). Returns (ok, message)."""
    done = set()
    extracted_any = False
    for depth in range(3 if nested else 1):
        todo = [a for a in find_archives(d) if a[1] not in done]
        if not todo:
            break
        for kind, path in todo:
            done.add(path)
            if not os.path.exists(path):
                continue
            log_cb(f"Extracting {os.path.basename(path)}")
            code, out = extract_one(kind, path, os.path.dirname(path), password)
            if code != 0:
                msg = out.strip().splitlines()[-1] if out.strip() else "unknown error"
                if "password" in out.lower() or "encrypted" in out.lower():
                    msg = "Archive is password protected (set a password on the job and retry)"
                return False, f"Extraction failed for {os.path.basename(path)}: {msg}"
            extracted_any = True
            if delete_archives:
                for v in volumes_of(kind, path):
                    try:
                        os.remove(v)
                    except OSError:
                        pass
    return (True, "Extracted") if extracted_any else (None, "No archives found")


def cleanup(d, delete_par2=True):
    for f in list_files(d):
        low = f.lower()
        if f.endswith(".state.json") or (delete_par2 and low.endswith(".par2")) or low.endswith(".sfv") \
                or low.endswith(".nzb.tmp"):
            try:
                os.remove(f)
            except OSError:
                pass


def unique_path(p):
    if not os.path.exists(p):
        return p
    i = 1
    while os.path.exists(f"{p}.{i}"):
        i += 1
    return f"{p}.{i}"


def move_to_complete(work_dir, complete_root, category, name):
    dest_root = os.path.join(complete_root, safe_name(category)) if category else complete_root
    os.makedirs(dest_root, exist_ok=True)
    dest = unique_path(os.path.join(dest_root, safe_name(name)))
    shutil.move(work_dir, dest)
    return dest


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:200] or "download"
