#!/usr/bin/env python3
"""Tests for the batch mode of pico/tools/pico_anonymize.py, against the real protocol code under qemu-riscv32.

A root folder holds good series, a series the device refuses (a missing slice), a folder holding two series, an empty
folder, a folder whose name is not <subject>_<sequence>, and an already finished case. Checked:
  * every case gets the right status; only the good ones produce output, and each output is exactly one .nii and one
    .json equal to what a single run gives
  * one failure does not stop the others; --stop-on-error does; losing the device stops the batch and the cases after
    it are reported as not run
  * resume: a second run skips what is done and retries what failed; a result folder missing a file is redone
  * --only, --manifest (labels, site, scanner, series number; invalid rows), --report, the exit codes
  * the whole batch uses ONE connection (one emulator process)
  * option checks (labels given together with --batch, batch options without it)
"""
import csv, importlib.util, json, os, shlex, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cl = load("pico_anonymize", os.path.join(ROOT, "pico", "tools", "pico_anonymize.py"))
pf, ds = cl.pf, cl.ds
gen = load("dicomgen", os.path.join(ROOT, "tests", "dicomgen.py"))
EMU = "qemu-riscv32 " + os.path.join(ROOT, "build32", "anon_test")
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failures += not ok


def tree(path):
    out = []
    for dp, dn, fn in os.walk(path):
        out += [os.path.relpath(os.path.join(dp, x), path) + "/" for x in dn] + [os.path.relpath(os.path.join(dp, x), path) for x in fn]
    return sorted(out)


def write(folder, files):
    os.makedirs(folder, exist_ok=True)
    for k, f in enumerate(files):
        open(os.path.join(folder, "IM%04d.dcm" % k), "wb").write(f)


def vals(rows, cols, k):
    return lambda j: [((i * 31 + (j + k) * 977) * 2654435761 >> 5) % 4000 for i in range(rows * cols)]


def status(results):
    return {r["folder"]: r["status"] for r in results}


def batch(root, out, cmd=EMU, **kw):
    link = pf.PipeLink(cmd)
    try:
        cases = cl.discover(root, kw.pop("manifest", None), kw.pop("only", None), kw.pop("site", "NA"), kw.pop("scanner", "NA"))
        return cl.run_batch(link, root, out, cases, settle=0, **kw)
    finally:
        link.close()


def cli(*args):
    r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "pico", "tools", "pico_anonymize.py")] + list(args),
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout, r.stderr


def main():
    if not os.path.exists(os.path.join(ROOT, "build32", "anon_test")):
        sys.exit("build the emulator protocol first: make build32/anon_test (or make test-batch)")
    work = tempfile.mkdtemp(prefix="batch-test-")
    pf.TIMEOUT = 8.0
    try:
        run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)


def run(work):
    root, out = os.path.join(work, "root"), os.path.join(work, "out")
    A = gen.stack(5, rows=12, cols=14, uid=b"1.1", vals=vals(12, 14, 0))
    B = gen.stack(4, rows=10, cols=11, uid=b"1.2", vals=vals(10, 11, 7), slope=2.5, inter=-100)
    C = gen.stack(6, rows=9, cols=9, uid=b"1.3", vals=vals(9, 9, 3))
    two = gen.stack(3, rows=8, cols=8, uid=b"2.1", vals=vals(8, 8, 1)) + gen.stack(3, rows=8, cols=8, uid=b"2.2", vals=vals(8, 8, 2), origin=(0, 0, 300))
    write(os.path.join(root, "S001_T1c"), A)
    write(os.path.join(root, "S001_T1"), B)
    write(os.path.join(root, "S002_FLAIR"), A)
    write(os.path.join(root, "S003_T1c"), C[:3] + C[4:])               # a missing slice: the device refuses it
    write(os.path.join(root, "S004_T2"), two)                          # two series in one folder
    os.makedirs(os.path.join(root, "S005_T2"))                         # empty
    write(os.path.join(root, "weird folder name"), A)                  # not <subject>_<sequence>
    write(os.path.join(root, "S006_T1c"), A)
    open(os.path.join(root, "README.txt"), "w").write("a file, not a folder")
    write(os.path.join(root, ".fetch-abc123"), A)                      # a leftover temporary folder of the downloader: never a case

    # ---- the first run
    res = batch(root, out)
    st = status(res)
    want = {"S001_T1": "ok", "S001_T1c": "ok", "S002_FLAIR": "ok", "S003_T1c": "failed", "S004_T2": "failed", "S005_T2": "failed",
            "S006_T1c": "ok", "weird folder name": "skipped"}
    check("each case gets the right status: good ones ok, the refused series, the two-series folder and the empty folder failed, the oddly named folder skipped",
          st == want, str(st))
    why = {r["folder"]: r["detail"] for r in res}
    check("the reasons are stated: GEOM:UNEVEN for the missing slice, 'choose one with --series' for two series, 'no DICOM files' for the empty folder, the naming rule for the odd name",
          "GEOM:UNEVEN" in why["S003_T1c"] and "--series" in why["S004_T2"] and "no DICOM files" in why["S005_T2"] and "<subject>_<sequence>" in why["weird folder name"],
          str(why))
    check("every detail is one line (the table stays one row per case), and the summary line counts 4 ok, 1 skipped, 3 failed, 0 not run",
          all("\n" not in r["detail"] for r in res) and cl.summary(res).splitlines()[-1] == "4 ok, 1 skipped, 3 failed, 0 not run",
          cl.summary(res).splitlines()[-1])
    check("one failure did not stop the cases after it (S006_T1c, after the failures, is ok)", st["S006_T1c"] == "ok")
    want_tree = sorted(sum([["%s/" % n, "%s/%s.json" % (n, n), "%s/%s.nii" % (n, n)] for n in ("S001_T1", "S001_T1c", "S002_FLAIR", "S006_T1c")], []))
    check("the output tree is exactly one folder per good case with one .nii and one .json (nothing for the failed cases, no temporary folders)",
          tree(out) == want_tree, str(tree(out)))
    # equal to a single run of the same series
    single = os.path.join(work, "single")
    link = pf.PipeLink(EMU)
    try:
        cl.anonymize(link, os.path.join(root, "S001_T1"), "S001", "T1", "NA", "NA", single, settle=0)
    finally:
        link.close()
    same = all(open(os.path.join(out, "S001_T1", "S001_T1" + e), "rb").read() == open(os.path.join(single, "S001_T1", "S001_T1" + e), "rb").read() for e in (".nii", ".json"))
    check("a case done in a batch is byte-identical to the same series done on its own", same)

    # ---- resume
    res2 = batch(root, out)
    st2 = status(res2)
    done_skipped = sorted(r["folder"] for r in res2 if r["status"] == "skipped" and r["detail"] == "already done")
    check("a second run skips the four finished cases ('already done') and tries the failed ones again",
          done_skipped == ["S001_T1", "S001_T1c", "S002_FLAIR", "S006_T1c"] and st2["S003_T1c"] == "failed" and st2["S004_T2"] == "failed", str(st2))
    victim = os.path.join(out, "S001_T1c", "S001_T1c.json")
    before = open(victim, "rb").read()
    os.unlink(victim)
    res3 = batch(root, out, only="S001_T1c")
    check("a result folder missing a file is redone (restored identical), not skipped and not refused as 'already exists'",
          status(res3) == {"S001_T1c": "ok"} and open(victim, "rb").read() == before, str(res3))
    # fix the broken ones and finish: only they run
    shutil.rmtree(os.path.join(root, "S003_T1c"))
    write(os.path.join(root, "S003_T1c"), C)
    res4 = batch(root, out)
    st4 = status(res4)
    check("after the missing slice is restored, a further run does only that case (the others are skipped)",
          st4["S003_T1c"] == "ok" and sum(1 for v in st4.values() if v == "ok") == 1 and sum(1 for v in st4.values() if v == "skipped") == 5, str(st4))
    res5 = batch(root, out, overwrite=True, only="S00[12]_*")
    check("--overwrite redoes finished cases", sorted(status(res5).items()) == [("S001_T1", "ok"), ("S001_T1c", "ok"), ("S002_FLAIR", "ok")], str(res5))

    # ---- filters, manifest, report
    check("--only filters by folder name", sorted(status(batch(root, os.path.join(work, "o2"), only="*_T1c")).items()) ==
          [("S001_T1c", "ok"), ("S003_T1c", "ok"), ("S006_T1c", "ok")])
    man = os.path.join(work, "manifest.csv")
    with open(man, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["folder", "subject", "sequence", "site", "scanner", "series"])
        w.writerow(["weird folder name", "CASE9", "t1_post", "SITE7", "SCAN2", ""])
        w.writerow(["S004_T2", "CASE10", "T2", "", "", "2"])
        w.writerow(["S001_T1", "bad label!", "T1", "", "", ""])
        w.writerow(["S002_FLAIR", "CASE11", "FLAIR", "", "", "x"])
    o3 = os.path.join(work, "o3")
    res6 = batch(root, o3, manifest=man, site="DEFSITE", scanner="DEFSCAN")
    st6 = status(res6)
    j = json.load(open(os.path.join(o3, "CASE9_t1_post", "CASE9_t1_post.json")))
    j2 = json.load(open(os.path.join(o3, "CASE10_T2", "CASE10_T2.json")))
    check("a manifest names the folders and labels: the odd folder name works, site and scanner come from the row or the defaults, series 2 of a two-series folder is selected",
          st6 == {"weird folder name": "ok", "S004_T2": "ok", "S001_T1": "failed", "S002_FLAIR": "failed"} and j["labels"] == {"subject": "CASE9", "sequence": "t1_post", "site": "SITE7", "scanner": "SCAN2"}
          and j2["labels"]["site"] == "DEFSITE" and j2["labels"]["scanner"] == "DEFSCAN" and j2["measured"]["size"][2] == 3, str(st6))
    check("invalid manifest rows (a bad label, a non-numeric series) fail by themselves and nothing is created for them",
          not os.path.exists(os.path.join(o3, "bad label!_T1")) and "not 1 to 32" in {r["folder"]: r["detail"] for r in res6}["S001_T1"]
          and "not a number" in {r["folder"]: r["detail"] for r in res6}["S002_FLAIR"], str({r["folder"]: r["detail"] for r in res6}))
    check("a manifest that does not exist is refused with a clear message", "cannot read" in (cl_err(lambda: cl.discover(root, os.path.join(work, "nope.csv"))) or ""))
    bad_man = os.path.join(work, "bad.csv")
    open(bad_man, "w").write("a,b\n1,2\n")
    check("a manifest with the wrong header is refused with a clear message", "folder,subject,sequence" in (cl_err(lambda: cl.discover(root, bad_man)) or ""))

    # ---- one connection for the whole batch
    counter = os.path.join(work, "starts")
    wrapper = os.path.join(work, "wrap.sh")
    open(wrapper, "w").write("#!/bin/sh\necho x >> %s\nexec %s\n" % (shlex.quote(counter), EMU))
    os.chmod(wrapper, 0o755)
    batch(root, os.path.join(work, "o4"), cmd=wrapper)
    check("the whole batch (8 cases) uses ONE connection: the device process was started once", open(counter).read().count("x") == 1, open(counter).read())

    # ---- losing the device, and --stop-on-error
    dead = batch(root, os.path.join(work, "o5"), cmd="true")
    stt = [r["status"] for r in dead]
    check("when the device is gone the first case fails and every case after it is reported 'not run' (not tried and failed one by one)",
          stt[0] == "failed" and set(stt[1:]) <= {"not run", "skipped"} and "not run" in stt and "lost the device" in dead[0]["detail"], str(dead[:3]))
    n_notrun = sum(1 for r in dead if r["status"] == "not run")
    check("the summary line counts the cases that were not run", n_notrun >= 5 and cl.summary(dead).splitlines()[-1].endswith(", %d not run" % n_notrun),
          cl.summary(dead).splitlines()[-1])
    shutil.rmtree(os.path.join(root, "S003_T1c"))
    write(os.path.join(root, "S003_T1c"), C[:3] + C[4:])
    so = batch(root, os.path.join(work, "o7"), stop_on_error=True, only="S00[3-6]_*")
    s7 = [(r["folder"], r["status"]) for r in so]
    check("--stop-on-error stops at the first failure: the cases after it are 'not run'",
          s7[0] == ("S003_T1c", "failed") and all(s == "not run" for _, s in s7[1:]) and len(s7) == 4, str(s7))

    # ---- the command line
    oc = os.path.join(work, "oc")
    rc, so_, se = cli(root, "--batch", "--out", oc, "--exec", EMU, "--report", os.path.join(work, "rep.csv"))
    rows = list(csv.DictReader(open(os.path.join(work, "rep.csv"))))
    check("command line: exit code 2 when a case failed, the table on stdout, the report CSV has one row per case",
          rc == 2 and "S001_T1c" in so_ and "failed" in so_ and len(rows) == 8 and {r["status"] for r in rows} <= {"ok", "failed", "skipped", "not run"}, "%s %s" % (rc, se[-200:]))
    shutil.rmtree(os.path.join(root, "S003_T1c"))
    write(os.path.join(root, "S003_T1c"), C)
    shutil.rmtree(os.path.join(root, "S004_T2"))
    shutil.rmtree(os.path.join(root, "S005_T2"))
    rc, so_, se = cli(root, "--batch", "--out", oc, "--exec", EMU)
    check("command line: exit code 0 when nothing failed (finished and skipped cases only)", rc == 0 and "0 failed" in so_, "%s %s" % (rc, so_[-200:] + se[-200:]))
    for args, frag in [((root, "--batch", "--subject", "x"), "do not give --subject"), ((root, "--batch", "--sequence", "x"), "do not give"),
                       ((root, "--batch", "--timing"), "do not give"), ((root, "--subject", "a", "--sequence", "b", "--only", "x"), "belong to --batch"),
                       ((root, "--subject", "a", "--sequence", "b", "--report", "x"), "belong to --batch"), ((root, "--subject", "a"), "give --subject and --sequence"),
                       ((root, "--batch", "--site", "bad site"), "not 1 to 32")]:
        rc, so_, se = cli(*args, "--exec", EMU, "--out", os.path.join(work, "oerr"))
        if rc != 1 or frag not in se or os.path.exists(os.path.join(work, "oerr")):
            check("command line option check: %s" % " ".join(args[1:]), False, "%s %s" % (rc, se[-150:]))
            break
    else:
        check("7 wrong option combinations are refused with exit 1 and a clear message, and create nothing", True)
    empty = os.path.join(work, "empty_root")
    os.makedirs(empty)
    rc1, _, se1 = cli(empty, "--batch", "--exec", EMU, "--out", os.path.join(work, "oempty"))
    rc2, _, se2 = cli(root, "--batch", "--only", "NO_SUCH_*", "--exec", EMU, "--out", os.path.join(work, "oempty"))
    check("an empty batch root, or an --only that matches nothing, is exit 1 'no cases found' (never a silent success)",
          rc1 == 1 and rc2 == 1 and "no cases found" in se1 and "no cases found" in se2 and not os.path.exists(os.path.join(work, "oempty")), "%s %s" % (rc1, rc2))
    rc, so_, se = cli(os.path.join(work, "empty_root_does_not_exist"), "--batch", "--exec", EMU)
    check("a missing batch root is exit 1, not a traceback", rc == 1 and "Traceback" not in se, se[-150:])


def cl_err(fn):
    try:
        fn()
    except cl.AnonError as e:
        return str(e)
    return None


main()
