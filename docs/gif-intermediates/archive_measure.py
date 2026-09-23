"""Full-archive measurement: AV1 4:4:4 masters for a site repository,
H.264 made from them at build time, against H.264 made from the gifs.

usage: pixi run --manifest-path docs/gif-intermediates/pixi.toml \
           python docs/gif-intermediates/archive_measure.py OUTDIR

Per animated gif in archive/raw (largest first, jobs in parallel, one
thread each):
  direct            H.264 exactly as sites.py _encode_video makes it
  <master>          an AV1 4:4:4 master (MASTERS below), frame-rate
                    capped as sites.py caps
  <master>.h264     H.264 made from that master with sites.py's settings
  av1_444_crf24.av1_420_crf24
                    AV1 4:2:0 made from the CRF 24 master
Every output is scored against the gif with quality.py and its duration
checked. One JSON line per output in OUTDIR/results.jsonl; rerunning
skips outputs already recorded.
"""
import ast
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
QUALITY = REPO / "docs/gif-intermediates/quality.py"

# kept_frames, select_frames and their constants, verbatim from sites.py
# without importing its dependencies
_ns = {}
for _n in ast.parse((REPO / "src/medium_archive/sites.py").read_text()).body:
    if (isinstance(_n, ast.FunctionDef)
            and _n.name in ("kept_frames", "select_frames")) or (
            isinstance(_n, ast.Assign)
            and getattr(_n.targets[0], "id", "") in ("EVEN_PAD",
                                                     "MIN_FRAME_MS")):
        exec(compile(ast.Module([_n], []), "sites.py", "exec"), _ns)
EVEN_PAD, kept_frames, select_frames = (
    _ns["EVEN_PAD"], _ns["kept_frames"], _ns["select_frames"])

FF = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
TAIL = ["-threads", "1", "-fps_mode", "passthrough",
        "-enc_time_base", "1:1000", "-an", "-movflags", "+faststart"]
# sites.py _encode_video's codec arguments
H264 = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-crf", "24", "-preset", "slower", "-bf", "0"]


def aom(crf, params=None):
    return (["-c:v", "libaom-av1", "-cpu-used", "6", "-g", "9999",
             "-row-mt", "0", "-crf", str(crf), "-pix_fmt", "yuv444p"]
            + (["-aom-params", params] if params else []))


MASTERS = {
    "av1_444_crf24": aom(24),
    "av1_444_crf28": aom(28),
}

# what each master is made into at build time: H.264 for every master,
# and from the CRF 24 master also AV1 4:2:0 (Main profile, what hardware
# AV1 decoders play), at the CRF that matched H.264 CRF 24 in the sample
AV1_420 = [a if a != "yuv444p" else "yuv420p" for a in aom(24)]
DERIVED = {"av1_444_crf24": [(".h264", H264), (".av1_420_crf24", AV1_420)]}

_lock = threading.Lock()


def delays(gif):
    with Image.open(gif) as im:
        if getattr(im, "n_frames", 1) < 2:
            return None
        out = []
        for i in range(im.n_frames):
            im.seek(i)
            out.append(im.info.get("duration", 0))
        return out


def duration_ms(path):
    run = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return round(float(run.stdout.strip()) * 1000)
    except ValueError:
        return None


def score(gif, out):
    run = subprocess.run([sys.executable, str(QUALITY), str(gif), str(out)],
                         capture_output=True, text=True)
    if run.returncode:
        return {"score_error": run.stderr.strip()[-300:]}
    return json.loads(run.stdout)


def record(res, rec):
    with _lock:
        with open(res, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    print(rec["kind"], Path(rec["gif"]).parent.parent.name,
          Path(rec["gif"]).name[:24], rec.get("out_bytes"), rec.get("enc_s"),
          rec.get("psnr"), rec.get("error", "")[:80], flush=True)


def encode(src, out, vf, codec):
    out.parent.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    run = subprocess.run(FF + ["-i", str(src)]
                         + (["-vf", ",".join(vf)] if vf else [])
                         + codec + TAIL + [str(out)],
                         capture_output=True, text=True)
    s = round(time.perf_counter() - t, 1)
    if run.returncode or not out.exists() or not out.stat().st_size:
        return s, (run.stderr or "").strip()[-300:] or "no output"
    return s, None


def finish(res, gif, gif_ms, kind, out, s, err):
    rec = {"gif": str(gif), "kind": kind, "gif_bytes": gif.stat().st_size,
           "gif_ms": gif_ms, "enc_s": s}
    if err:
        rec["error"] = err
    else:
        rec["out_bytes"] = out.stat().st_size
        rec["out_ms"] = duration_ms(out)
        rec.update(score(gif, out))
    record(res, rec)
    return not err


def job(outdir, res, gif, d, kind, todo):
    name = gif.parent.parent.name + "__" + gif.stem + ".mp4"
    gif_ms = sum(d)
    keep = kept_frames(d)
    select = [select_frames(keep)] if len(keep) < len(d) else []
    if kind == "direct":
        out = outdir / "direct" / name
        finish(res, gif, gif_ms, "direct", out,
               *encode(gif, out, select + [EVEN_PAD], H264))
        return
    master = outdir / kind / name
    if kind in todo:
        if not finish(res, gif, gif_ms, kind, master,
                      *encode(gif, master, select, MASTERS[kind])):
            return
    for suffix, codec in DERIVED.get(kind, [(".h264", H264)]):
        if kind + suffix in todo:
            out = outdir / (kind + suffix) / name
            finish(res, gif, gif_ms, kind + suffix, out,
                   *encode(master, out, [EVEN_PAD], codec))


def main():
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    res = outdir / "results.jsonl"
    done = set()
    if res.exists():
        for line in res.read_text().splitlines():
            r = json.loads(line)
            if "out_bytes" in r and "score_error" not in r:
                done.add((r["gif"], r["kind"]))
    gifs = sorted((REPO / "archive/raw").rglob("*.gif"),
                  key=lambda p: -p.stat().st_size)
    jobs = []
    for g in gifs:
        d = delays(g)
        if d is None:
            print("still, skipped:", g, flush=True)
            continue
        for kind in [*MASTERS, "direct"]:
            wanted = [kind] + [kind + sfx for sfx, _ in
                               DERIVED.get(kind, [(".h264", None)])] \
                if kind != "direct" else [kind]
            todo = {k for k in wanted if (str(g), k) not in done}
            if todo:
                jobs.append((g, d, kind, todo))
    # the direct h264 baseline first (fast), then AV1, largest gifs
    # first, so the pool drains evenly
    jobs.sort(key=lambda j: (j[2] != "direct", -j[0].stat().st_size))
    print(len(jobs), "jobs", flush=True)
    with ThreadPoolExecutor(int(os.environ.get("JOBS", os.cpu_count()))) as pool:
        futs = [pool.submit(job, outdir, res, *j) for j in jobs]
        for f in as_completed(futs):
            f.result()
    print("done", flush=True)


if __name__ == "__main__":
    main()
