from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import median
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
from scipy.io import wavfile
from scipy.optimize import linear_sum_assignment

from secure_audio.benchmark import demo_signal
from secure_audio.decoder import SAMPLE_RATE, SubprocessDecoder
from secure_audio.fingerprint import MemoryIndex, fingerprint, retrieve, timed_matches
from secure_audio.scoring import LogisticModel


ROOT = Path(__file__).resolve().parents[1]
PEAKFP_COMMIT = "365df239bae4cea155232f09e1ca3b6b98a060c3"
PANAKO_SHA256 = "767cdd2cd0991658c4a25a0b8e887f9a2a38f69ae17781b02fe1652e1a7173d4"
MIN_IOU = 0.5
PANAKO_CONFIG = {
    "STRATEGY": "PANAKO",
    "PANAKO_SAMPLE_RATE": 8000,
    "PANAKO_AUDIO_BLOCK_SIZE": 4096,
    "PANAKO_TRANSF_TIME_RESOLUTION": 64,
}


@dataclass(frozen=True)
class Cue:
    clip: str
    recording: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Prediction(Cue):
    offset_ms: int


def samples(start, end):
    return slice(start * SAMPLE_RATE, end * SAMPLE_RATE)


def fixtures(directory, original):
    directory.mkdir(parents=True, exist_ok=True)
    references = {}
    tracks = {name: demo_signal(seed) for name, seed in zip("abc", (41, 76, 123), strict=True)}
    for name, signal in tracks.items():
        path = directory / f"reference-{name}.wav"
        wavfile.write(path, SAMPLE_RATE, signal)
        references[name] = path

    broadcast = np.zeros(53 * SAMPLE_RATE, dtype=np.float32)
    broadcast[samples(3, 11)] = broadcast[samples(17, 25)] = tracks["a"][samples(2, 10)]
    broadcast[samples(28, 36)] = tracks["b"][samples(2, 10)]
    broadcast[samples(40, 47)] = 0.7 * (tracks["a"][samples(1, 8)] + tracks["b"][samples(1, 8)])
    rate, prior = wavfile.read(original)
    if rate != SAMPLE_RATE or not np.array_equal(prior, broadcast):
        raise ValueError("The original broadcast fixture differs from its benchmark ground truth")
    queries = {"broadcast": directory / "broadcast.wav"}
    queries["broadcast"].write_bytes(original.read_bytes())

    distractor = demo_signal(205, 53)
    stress = np.zeros_like(broadcast)
    stress[samples(6, 14)] = tracks["b"][samples(2, 10)] * 0.25
    masked = tracks["a"][samples(2, 10)]
    bed = distractor[samples(24, 32)]
    scale = np.linalg.norm(bed) / np.linalg.norm(masked) * 10 ** (-6 / 20)
    stress[samples(24, 32)] = bed + scale * masked
    stress[samples(34, 37)] = tracks["c"][samples(2, 5)]
    for name, signal in {"short-and-masked": stress, "out-of-catalog": distractor,
                         "silence": np.zeros_like(broadcast)}.items():
        path = directory / f"{name}.wav"
        wavfile.write(path, SAMPLE_RATE, signal)
        queries[name] = path

    gold = [Cue(clip, track, start * 1000, end * 1000) for clip, track, start, end in (
        ("broadcast", "a", 3, 11), ("broadcast", "a", 17, 25), ("broadcast", "b", 28, 36),
        ("broadcast", "a", 40, 47), ("broadcast", "b", 40, 47), ("short-and-masked", "b", 6, 14),
        ("short-and-masked", "a", 24, 32), ("short-and-masked", "c", 34, 37),
    )]
    return references, queries, gold


def run(command, log=None, timeout=240):
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    if log is not None:
        log.with_suffix(".stdout.txt").write_text(result.stdout, encoding="utf-8")
        log.with_suffix(".stderr.txt").write_text(result.stderr, encoding="utf-8")
    result.check_returncode()
    return result.stdout


def secure_audio(references, queries, model):
    decoder = SubprocessDecoder()

    def decode(path):
        return decoder.decode(path.read_bytes(), 60)

    with decoder.prewarm():
        index = MemoryIndex({name: fingerprint(decode(path)) for name, path in references.items()})
        predictions, review = [], []
        for clip, path in queries.items():
            confirmed, uncertain = timed_matches(retrieve(decode(path), index), model)
            for segments, destination in ((confirmed, predictions), (uncertain, review)):
                for segment in segments:
                    payload = segment.payload()
                    start = payload["query_start_ms"]
                    destination.append(Prediction(clip, segment.reference_id, start, payload["query_end_ms"],
                                                  payload["reference_start_ms"] - start))
    return predictions, review


PEAKFP_IMPORTS = """
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from extractor import extract_peakfp
"""


PEAKFP_INDEX = PEAKFP_IMPORTS + """
from indexer import create_index

references = json.loads(sys.argv[2])
directory = Path(sys.argv[3])
fingerprints = []
for key, audio in references.items():
    path = directory / ('ref-' + key + '.pkl')
    extract_peakfp(audio, str(path), 5)
    fingerprints.append(str(path))
ref_list = directory / 'refs.txt'
ref_list.write_text('\\n'.join(fingerprints), encoding='utf-8')
create_index(ref_list, directory / 'index.pkl')
"""


PEAKFP_QUERY = PEAKFP_IMPORTS + """
from matcher import run_matching
from utils import load_pickle

clip, audio, scratch = sys.argv[2:]
directory = Path(scratch)
path = directory / (clip + '.pkl')
extract_peakfp(audio, str(path), 5)
has_peaks = bool(len(load_pickle(path)))
matches = run_matching((path, directory / 'index.pkl', 5, 600, 300)) if has_peaks else []
print(json.dumps({'matches': matches, 'empty_fingerprint': not has_peaks}))
"""


def peakfp(references, queries, python, repository, logs):
    commit = run(["git", "-C", str(repository), "rev-parse", "HEAD"]).strip()
    dirty = run(["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=no"]).strip()
    if commit != PEAKFP_COMMIT or dirty:
        raise ValueError(f"Expected an unmodified PeakFP checkout at {PEAKFP_COMMIT}")
    inputs = json.dumps({name: str(path) for name, path in references.items()})
    source = str(repository / "peakfp")
    with TemporaryDirectory() as scratch:
        run([str(python), "-c", PEAKFP_INDEX, source, inputs, scratch], logs / "peakfp-index")
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            tasks = {clip: pool.submit(run, [str(python), "-c", PEAKFP_QUERY, source, clip, str(path), scratch],
                                       logs / f"peakfp-{clip}", timeout=900) for clip, path in queries.items()}
            outputs = {clip: task.result() for clip, task in tasks.items()}
    return parse_peakfp(outputs)


def parse_peakfp(outputs):
    predictions, empty = [], []
    for clip, output in outputs.items():
        raw = json.loads(output.splitlines()[-1])
        if raw["empty_fingerprint"]:
            empty.append(clip)
        for row in raw["matches"]:
            predictions.append(Prediction(clip, Path(row["ref"]).stem.removeprefix("ref-"),
                                          round(row["query_start"] * 1000), round(row["query_end"] * 1000),
                                          round((row["ref_start"] - row["query_start"]) * 1000)))
    return predictions, empty


def parse_panako(output, references, queries, window, overlap):
    predictions = []
    windows = {name: set() for name in queries}
    for line in output.splitlines():
        row = [value.strip() for value in line.split(";")]
        if len(row) != 13 or not row[0].isdigit():
            continue
        match = re.fullmatch(r"(.+)\.wav-(\d+\.\d+)_(\d+\.\d+)", row[2])
        if match is None:
            raise ValueError(f"Unexpected Panako query path {row[2]}")
        clip = Path(match[1]).name
        window_start, window_end = (float(match[index]) for index in (2, 3))
        windows[clip].add(window_start)
        if window_end - window_start != window:
            raise ValueError(f"Unexpected Panako monitor window {row[2]}")
        if row[5] in ("", "null"):
            continue
        recording = Path(row[5]).stem.removeprefix("reference-")
        if recording not in references:
            raise ValueError(f"Unexpected Panako reference {row[5]}")
        start, end = (round(1000 * (float(row[index]) + window_start)) for index in (3, 4))
        predictions.append(Prediction(clip, recording, start, end, round(1000 * float(row[7])) - start))
    for clip, path in queries.items():
        rate, signal = wavfile.read(path, mmap=True)
        expected = set(range(0, int(np.ceil(len(signal) / rate - window)), window - overlap))
        if windows[clip] != expected:
            raise ValueError(f"Incomplete Panako results for {clip}: {windows[clip]} instead of {expected}")
    return merge_panako(predictions)


def merge_panako(predictions):
    merged = []
    for current in sorted(predictions, key=lambda p: (p.clip, p.recording, p.start_ms)):
        for index, prior in enumerate(merged):
            if (prior.clip == current.clip and prior.recording == current.recording
                    and current.start_ms <= prior.end_ms and abs(current.offset_ms - prior.offset_ms) <= 250):
                merged[index] = Prediction(prior.clip, prior.recording, prior.start_ms,
                                           max(prior.end_ms, current.end_ms), prior.offset_ms)
                break
        else:
            merged.append(current)
    return merged


def panako(references, queries, jar, wsl_distribution, logs):
    if sha256(jar.read_bytes()).hexdigest() != PANAKO_SHA256:
        raise ValueError("Unexpected Panako v2.1.0 release JAR digest")
    prefix = ["wsl.exe", "-d", wsl_distribution, "--"] if wsl_distribution else []

    def host_path(path):
        return run([*prefix, "wslpath", "-a", str(path)]).strip() if prefix else str(path)

    scratch = run([*prefix, "mktemp", "-d", "/tmp/secure-audio-benchmark.XXXXXXXX"]).strip()
    environment = ["env", "PATH=/usr/local/bin:/usr/bin:/bin"] if prefix else []
    java = [*prefix, *environment, "java", "--add-opens=java.base/java.nio=ALL-UNNAMED",
            "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED", "-Duser.language=en", "-Duser.country=US", f"-Duser.home={scratch}",
            f"-Djava.io.tmpdir={scratch}", "-jar", host_path(jar)]
    config = [*(f"{key}={value}" for key, value in PANAKO_CONFIG.items()),
              f"PANAKO_LMDB_FOLDER={scratch}/db", f"PANAKO_CACHE_FOLDER={scratch}/cache",
              f"DECODER_PIPE_LOG_FILE={scratch}/decoder.log"]
    ref_files = [host_path(path) for path in references.values()]
    query_files = [host_path(path) for path in queries.values()]
    run([*java, "store", *config, *ref_files], logs / "panako-store")
    stats = run([*java, "stats", *config], logs / "panako-stats")
    if not re.search(r"> 3 audio files\b", stats):
        raise ValueError(f"Panako did not index all three references: {stats}")
    results = {}
    for window, overlap in ((12, 6), (25, 5)):
        settings = [f"MONITOR_STEP_SIZE={window}", f"MONITOR_OVERLAP={overlap}"]
        output = run([*java, "monitor", *config, *settings, *query_files], logs / f"panako-{window}-{overlap}")
        results[f"panako-{window}-{overlap}"] = parse_panako(output, references, queries, window, overlap)
    return results


def iou(a, b):
    if (a.clip, a.recording) != (b.clip, b.recording):
        return 0.0
    overlap = min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms)
    return max(0, overlap) / (max(a.end_ms, b.end_ms) - min(a.start_ms, b.start_ms))


def associate(gold, predictions):
    weights = np.zeros((len(gold), len(predictions)))
    for i, cue in enumerate(gold):
        for j, prediction in enumerate(predictions):
            overlap = iou(cue, prediction)
            if overlap >= MIN_IOU:
                weights[i, j] = 1 + len(gold) + overlap
    return [(i, j) for i, j in zip(*linear_sum_assignment(weights, maximize=True), strict=True) if weights[i, j]]


def score(gold, predictions, review=()):
    matches = associate(gold, predictions)
    matched_cues = {i for i, _ in matches}
    matched_predictions = {j for _, j in matches}
    missed = [cue for i, cue in enumerate(gold) if i not in matched_cues]
    false_alerts = [prediction for j, prediction in enumerate(predictions) if j not in matched_predictions]
    negative_false_alerts = [prediction for prediction in false_alerts if prediction.clip in {"silence", "out-of-catalog"}]
    overlapping_false_alerts = [prediction for prediction in false_alerts if any(iou(cue, prediction) > 0 for cue in gold)]
    timing = [{"cue": asdict(gold[i]), "prediction": asdict(predictions[j]), "iou": round(iou(gold[i], predictions[j]), 4),
               "start_error_ms": predictions[j].start_ms - gold[i].start_ms,
               "end_error_ms": predictions[j].end_ms - gold[i].end_ms} for i, j in matches]
    return {
        "confirmed_count": len(predictions), "matched_count": len(matches), "missed_count": len(missed),
        "false_alert_count": len(false_alerts), "negative_false_alert_count": len(negative_false_alerts),
        "false_alerts_overlapping_same_recording": len(overlapping_false_alerts),
        "median_absolute_start_error_ms": median(abs(row["start_error_ms"]) for row in timing) if timing else None,
        "median_absolute_end_error_ms": median(abs(row["end_error_ms"]) for row in timing) if timing else None,
        "matched_timings": timing, "missed_cues": [asdict(cue) for cue in missed],
        "false_alerts": [asdict(prediction) for prediction in false_alerts],
        "review_count": len(review), "review_candidates": [asdict(prediction) for prediction in review],
        "missed_cues_with_review_candidate": [asdict(missed[i]) for i, _ in associate(missed, review)],
    }


def write_report(output, gold, audio_paths, model, empty_queries, scores):
    report = {
        "ground_truth": [asdict(cue) for cue in gold], "minimum_iou": MIN_IOU,
        "fixture_sha256": {path.name: sha256(path.read_bytes()).hexdigest() for path in audio_paths},
        "secure_audio_model": {"version": model.version, "threshold": model.threshold,
                               "review_threshold": model.review_threshold},
        "peakfp": {"commit": PEAKFP_COMMIT, "peak_distance": 5, "threshold": 5, "window_frames": 600,
                   "hop_frames": 300, "empty_fingerprint_queries": empty_queries},
        "panako": {"release": "v2.1.0", "jar_sha256": PANAKO_SHA256, "configuration": PANAKO_CONFIG,
                   "monitor_window_and_overlap_seconds": [[12, 6], [25, 5]],
                   "monitor_coverage_seconds_per_53_second_clip": {"12-6": 48, "25-5": 45},
                   "merge_offset_tolerance_ms": 250},
        "results": scores,
    }
    path = output / "results.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(path)


def main():
    parser = ArgumentParser(description="Compare timed cue detection on generated broadcast fixtures")
    parser.add_argument("--peakfp-python", type=Path, required=True)
    parser.add_argument("--peakfp-repo", type=Path, required=True)
    parser.add_argument("--panako-jar", type=Path, required=True)
    parser.add_argument("--wsl-distribution")
    parser.add_argument("--original", type=Path, default=ROOT / "output/benchmark/broadcast-usage-fixture/broadcast.wav")
    parser.add_argument("--model", type=Path, default=ROOT / "output/benchmark/song-describer/logistic-model.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output/benchmark/broadcast-matcher-comparison")
    args = parser.parse_args()
    output = args.output.resolve()
    logs = output / "raw"
    logs.mkdir(parents=True, exist_ok=True)
    references, queries, gold = fixtures(output / "audio", args.original)
    model = LogisticModel.from_path(args.model)
    with ThreadPoolExecutor(max_workers=2) as pool:
        peak_run = pool.submit(peakfp, references, queries, args.peakfp_python.resolve(), args.peakfp_repo.resolve(), logs)
        panako_run = pool.submit(panako, references, queries, args.panako_jar.resolve(), args.wsl_distribution, logs)
        ours, review = secure_audio(references, queries, model)
        peaks, empty_queries = peak_run.result()
        panako_results = panako_run.result()
    scores = {"secure-audio": score(gold, ours, review), "peakfp": score(gold, peaks)}
    scores.update({name: score(gold, predictions) for name, predictions in panako_results.items()})
    write_report(output, gold, (*references.values(), *queries.values()), model, empty_queries, scores)


if __name__ == "__main__":
    main()
