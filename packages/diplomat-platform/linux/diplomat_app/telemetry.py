"""The telemetry ledger: what the monitors record, and the arithmetic over it.

The Linux twin of ``DiplomatCore/Telemetry.swift`` (the math) plus the ledger
writer that has no Swift counterpart here — macOS writes the same file from
``TelemetryLog.swift``. One append-only file,
``~/.diplomat/pr-monitor/telemetry.jsonl``, one JSON object per line, ``O_APPEND``
like the activity feed so two processes appending at once can't clobber each
other.

**Why a ledger and not counters.** Most of the screen's figures are about
*time* — how long work waited, how long it ran, how much was owed on each of the
last fourteen days — and a counter cannot be asked what it read last Tuesday. So
the monitors record events and every figure is derived on read. Nothing in the
file is a summary; a bug in the arithmetic is fixed by editing this module, not
by re-gathering a fortnight of data.

The event vocabulary, per unit of auto-work (keyed by the mesh work key, which is
already the identity two machines agree on — :func:`autofix.work_key`):

``queued``
    a poll saw this work owed for the first time.
``started``
    an agent was dispatched for it (``remote`` when the mesh placed it on a peer,
    whose quota it then spends rather than ours).
``done``
    the agent exited — timed from its completion sentinel, or from the last turn of
    its transcript where the mesh placed the run and kept no sentinel we can read —
    carrying the tokens that transcript accounts for (see :mod:`usagescan`).
``cleared``
    a poll no longer sees it owed and we never started it — someone replied by
    hand, the PR closed, a peer took it.

plus a ``sample`` every :data:`SAMPLE_INTERVAL_SECS` carrying the account's
remaining quota fractions and this machine's cumulative Claude token counters,
split monitored-repo vs everything else.

The quota fractions are a measurement, and the screen draws them as one
(:func:`quota_series`): both rate-limit windows over the lookback, resets and
all. They do double duty as the thing that makes *tokens* comparable to a limit —
Anthropic publishes a utilization percentage and never a token budget, so the
pair (quota consumed, tokens spent) over an interval is the only honest way to
price the window in tokens; see :func:`calibrate`.

The math half is a byte-for-byte twin of the Swift: ``diplomat-core telemetry``
prints the same figures and ``tests/test_telemetry_parity.py`` diffs the two.
Anything changed here must be changed there.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core

# MARK: - Shared model (assets/telemetry.json)


def model() -> dict:
    """The shared presentation model — ranges, chart resolutions, metric copy."""
    return core.telemetry()


def _model_get(key: str, default):
    try:
        return model().get(key, default)
    except Exception:  # noqa: BLE001 — a missing asset must not stop the monitors recording
        return default


#: How often a poll writes a quota/token sample. Read from the shared model so the
#: two platforms can't drift on ledger growth.
SAMPLE_INTERVAL_SECS = float(_model_get("sampleIntervalSecs", 900))
#: How far back the ledger is kept when it is rewritten.
RETAIN_DAYS = float(_model_get("retainDays", 60))
#: Size past which the next append rewrites the file to ``RETAIN_DAYS``.
MAX_LEDGER_BYTES = int(_model_get("maxLedgerBytes", 4 * 1024 * 1024))

#: Points the fitted normal is sampled at, per histogram bin (twin of
#: ``Telemetry.curveResolution``).
CURVE_RESOLUTION = 4


# MARK: - Ledger file


def _dir() -> Path:
    """The shared monitor directory — the same one the activity feed lives in, so
    a redirect for tests covers both."""
    from . import activity

    return activity._dir()


def ledger_path() -> Path:
    return _dir() / str(_model_get("ledgerFile", "telemetry.jsonl"))


def append(event: dict) -> None:
    """Append one event. Best-effort and atomic (``O_APPEND``); never raises into
    the caller, because a monitor poll must not fail over bookkeeping."""
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        path = ledger_path()
        _rotate_if_large(path)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _rotate_if_large(path: Path) -> None:
    """Rewrite the ledger to the retention horizon once it outgrows the cap.

    Not a plain truncate: the file is the only record of what was owed and when,
    so the rewrite keeps every event inside ``RETAIN_DAYS`` (the longest lookback
    the screen offers) and drops only what no range can reach. Written to a
    sibling and renamed, so a concurrent reader sees the old file or the new one
    and never a half-written one — and a concurrent *appender* at worst loses the
    events it wrote during the rewrite, which is why this runs at the cap rather
    than on a timer.
    """
    try:
        if path.stat().st_size <= MAX_LEDGER_BYTES:
            return
    except OSError:
        return
    cutoff = time.time() - RETAIN_DAYS * 86400
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        with open(path, encoding="utf-8", errors="replace") as src, \
                open(tmp, "w", encoding="utf-8") as dst:
            for line in src:
                try:
                    at = float(json.loads(line).get("at", 0))
                except (ValueError, TypeError, AttributeError):
                    continue
                if at >= cutoff:
                    dst.write(line if line.endswith("\n") else line + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


#: How much of the ledger's tail is read. Two months of 15-minute samples plus the
#: work events run well under a megabyte; the cap is the backstop for a file that
#: grew before a rotation could run.
_READ_TAIL_BYTES = 4 * 1024 * 1024


def read_lines() -> list[str]:
    """The ledger's tail as raw lines. A mid-file start lands mid-line, so the
    partial first line is dropped on the raw bytes before decoding (same reason
    :func:`activity.read` does it)."""
    try:
        with open(ledger_path(), "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = size - _READ_TAIL_BYTES if size > _READ_TAIL_BYTES else 0
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []


# MARK: - Recording (what the monitors call)


def record_queued(key: str, duty: str, pr: int) -> None:
    append({"at": time.time(), "ev": "queued", "key": key, "duty": duty, "pr": pr})


def record_started(key: str, remote: bool = False, attempt: int = 1) -> None:
    append({"at": time.time(), "ev": "started", "key": key,
            "remote": remote, "attempt": attempt})


def record_done(key: str, at: float, tokens: float | None) -> None:
    """Record a completion at ``at`` — when the agent actually exited, not when a
    poll noticed (which is up to a poll period later and would inflate every run
    time). :func:`record_completion` is what establishes that instant."""
    event = {"at": at, "ev": "done", "key": key}
    if tokens is not None:
        event["tokens"] = tokens
    append(event)


def record_cleared(key: str) -> None:
    append({"at": time.time(), "ev": "cleared", "key": key})


def record_completion(key: str, prompt: str, started_at: float,
                      exited_at: float | None, noticed_at: float) -> None:
    """Record a finished agent, pricing it from its own transcript and dating it
    from the best evidence of when it exited.

    ``exited_at`` is the completion sentinel's mtime, for a run that left one. A
    run the mesh placed leaves none: the node deletes its own sentinel the instant
    it fires, so the applet never sees it. The transcript's last turn stands in —
    written seconds before the agent exits, where ``noticed_at`` is the poll that
    found the run gone, up to a poll period later, and would inflate the run time
    by that much.

    Attribution can fail — the applet restarting mid-agent loses the prompt the
    match needs — and that is recorded honestly as a completion with no tokens
    rather than skipped, so the run/wait times still count it and the screen can
    say how many finished tasks it could not price.
    """
    from . import usagescan

    try:
        run = usagescan.task_run(prompt, started_at, exited_at or noticed_at)
    except OSError:
        run = None
    if exited_at is None:
        exited_at = run.last_turn_at if run is not None else noticed_at
    record_done(key, exited_at, run.tokens if run is not None else None)


# MARK: - Reading (what the monitors and the screen share)

#: The last fold, keyed by the ledger's (mtime, size). Both the 3-minute poll and
#: the screen fold the same file, and a repaint must not re-parse a megabyte —
#: while an append changes both parts of the key, so a stale fold is impossible.
_fold_cache: tuple[tuple[float, int], Ledger] | None = None


def load() -> Ledger:
    """The folded ledger, cached until the file changes. Never raises."""
    global _fold_cache
    try:
        st = ledger_path().stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        key = (0.0, 0)
    if _fold_cache is not None and _fold_cache[0] == key:
        return _fold_cache[1]
    ledger = fold(read_lines())
    _fold_cache = (key, ledger)
    return ledger


def _reset_cache() -> None:
    """Test hook: forget the cached fold (a test that rewrites the ledger inside
    one filesystem timestamp tick would otherwise read the previous one)."""
    global _fold_cache
    _fold_cache = None


def observe_owed(kind: str, duty: str, owed: dict[str, int]) -> None:
    """Reconcile one poll's owed set against the ledger: record work newly seen as
    owed, and clear work that stopped being owed before anyone started it.

    ``owed`` maps this poll's ledger keys to their PR numbers. Keys embed the head
    sha, so a fresh push is genuinely new work with a new key rather than an
    update to old work — which is why "already known" is enough to suppress a
    second ``queued``, and why a cleared key never comes back.

    ``kind`` scopes the reconciliation to the keys THIS poll is authoritative
    about, and ``duty`` is the coarser bucket the screen charts. The two differ
    for reviews, which arrive from two independent polls: replies owed on my own
    PRs (``review-reply``) and reviews requested of me (``review``). Both chart as
    "reviews", but scoping the sweep by duty alone would make each poll clear the
    other's pending work — the my-PRs poll declaring every outstanding review
    request resolved, twice a minute, because it never looked at one.

    Clearing is deliberately limited to work no agent ever took: an item that was
    started has an outcome of its own (its completion, or nothing, if the agent
    died), and marking it "cleared" as well would make it look like the monitor
    dropped work it actually did.
    """
    prefix = f"{kind}:"
    ledger = load()
    known = {t.key for t in ledger.tasks if t.key.startswith(prefix)}
    for key, pr in owed.items():
        if key and key not in known:
            record_queued(key, duty, pr)
    stale = {t.key for t in ledger.tasks
             if t.key.startswith(prefix) and t.queued_at is not None
             and t.started_at is None and t.cleared_at is None}
    for key in sorted(stale - set(owed)):
        record_cleared(key)


def sample_due(now: float | None = None) -> bool:
    """Whether it is time for another quota/token sample. Driven off the ledger's
    own last sample rather than a timer, so an applet that restarts every few
    minutes doesn't sample every launch."""
    now = time.time() if now is None else now
    samples = load().samples
    if not samples:
        return True
    return now - samples[-1].at >= SAMPLE_INTERVAL_SECS


def record_sample(session_left: float | None, week_left: float | None,
                  repo_tokens: float, other_tokens: float) -> None:
    append({"at": time.time(), "ev": "sample",
            "sessionLeft": session_left, "weekLeft": week_left,
            "repoTokens": repo_tokens, "otherTokens": other_tokens})


# MARK: - Fold


@dataclass
class Task:
    """One unit of auto-work, folded from every event carrying its key."""

    key: str
    duty: str = ""
    pr: int = 0
    queued_at: float | None = None
    started_at: float | None = None
    done_at: float | None = None
    cleared_at: float | None = None
    remote: bool = False
    tokens: float | None = None

    @property
    def run_secs(self) -> float | None:
        if self.started_at is None or self.done_at is None:
            return None
        return self.done_at - self.started_at if self.done_at >= self.started_at else None

    @property
    def wait_secs(self) -> float | None:
        if self.queued_at is None or self.started_at is None:
            return None
        return self.started_at - self.queued_at if self.started_at >= self.queued_at else None

    def pending(self, t: float) -> bool:
        """Whether it was owed (and unstarted) at ``t``. An unfinished task stays
        pending to the end of the range on purpose: it *was* owed for that whole
        stretch, including any span where the applet was off and nothing polled."""
        if self.queued_at is None or self.queued_at > t:
            return False
        if self.started_at is not None and self.started_at <= t:
            return False
        if self.cleared_at is not None and self.cleared_at <= t:
            return False
        return True


@dataclass(frozen=True)
class Sample:
    """One poll's reading of the account's quota and this machine's token
    counters. ``repo_tokens``/``other_tokens`` are cumulative and monotonic within
    a run of the scanner; a drop means its cursor file was lost and the counters
    restarted, which every consumer treats as a segment boundary, not a delta."""

    at: float
    session_left: float | None
    week_left: float | None
    repo_tokens: float
    other_tokens: float


@dataclass
class Ledger:
    tasks: list[Task] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)


def _number(raw: object) -> float | None:
    """A JSON number (or a numeric string from a hand-edited file) as a finite
    float. Non-finite values are rejected: one ``Infinity`` anywhere downstream
    turns every mean into ``nan``.

    A JSON ``true`` reads as 1.0, matching the Swift twin — there, a boolean
    bridges to ``NSNumber`` and the same cast accepts it. Nothing either writer
    emits puts a boolean in a numeric field; what matters is that a hand-edited
    file makes both platforms answer the same way.
    """
    if raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fold(lines: list[str]) -> Ledger:
    """Fold raw ledger lines into tasks and samples.

    Unparseable lines, unknown verbs and events missing ``at``/``key`` are
    skipped: the file is appended to by two platforms and a partially-written
    tail is normal, so one bad line must cost that line and nothing else.

    Repeat events for one key are first-wins on every instant, which is what
    makes a retry read correctly — attempt 2 appends a second ``started``, and
    the wait reported is still the wait until work actually began.
    """
    order: list[str] = []
    by_key: dict[str, Task] = {}
    samples: list[Sample] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        at = _number(obj.get("at"))
        ev = obj.get("ev")
        if at is None or not isinstance(ev, str):
            continue

        if ev == "sample":
            samples.append(Sample(
                at=at,
                session_left=_number(obj.get("sessionLeft")),
                week_left=_number(obj.get("weekLeft")),
                repo_tokens=_number(obj.get("repoTokens")) or 0.0,
                other_tokens=_number(obj.get("otherTokens")) or 0.0,
            ))
            continue

        key = obj.get("key")
        if not isinstance(key, str) or not key:
            continue
        # A verb this build doesn't understand — a newer platform's event, or a
        # corrupted line — must not conjure a timestamp-less task. Such a row
        # changes no figure, but it makes the key look already-recorded to
        # :func:`observe_owed`, which would then never queue that work at all.
        if ev not in ("queued", "started", "done", "cleared"):
            continue
        task = by_key.get(key)
        if task is None:
            task = Task(key=key)
            by_key[key] = task
            order.append(key)
        duty = obj.get("duty")
        if isinstance(duty, str) and duty:
            task.duty = duty
        pr = _number(obj.get("pr"))
        if pr is not None and pr > 0:
            task.pr = int(pr)
        if ev == "queued":
            if task.queued_at is None:
                task.queued_at = at
        elif ev == "started":
            if task.started_at is None:
                task.started_at = at
                task.remote = obj.get("remote") is True
        elif ev == "done":
            if task.done_at is None:
                task.done_at = at
                task.tokens = _number(obj.get("tokens"))
            elif not (task.tokens or 0) > 0:
                # A retry appends a SECOND completion under the same key. The
                # instants stay first-wins, but the price is taken from whichever
                # attempt could be attributed at all — otherwise a task whose first
                # attempt was never tied back to a transcript stays unpriced however
                # many times it is re-run.
                later = _number(obj.get("tokens"))
                if later is not None and later > 0:
                    task.tokens = later
        else:  # "cleared"
            if task.cleared_at is None:
                task.cleared_at = at

    # Samples arrive in append order, which is chronological — but two processes
    # (the applet and a mesh node) append to one file, so a slow write can land
    # out of order. Every consumer below walks consecutive pairs, so sort once.
    samples.sort(key=lambda s: s.at)
    return Ledger(tasks=[by_key[k] for k in order], samples=samples)


# MARK: - Calibration: what a rate-limit window is worth, in tokens


def calibrate(samples: list[Sample], *, session: bool) -> float | None:
    """Tokens per 100% of a rate-limit window, measured from consecutive samples.

    Over an interval the account spent ``d_util`` of its window while this machine
    logged ``d_tokens``, so the whole window is worth ``d_tokens / d_util``.
    Summing numerator and denominator across every usable interval weights long
    intervals more heavily, which is what you want: a 15-minute interval's
    rounding error should not count as much as an hour's.

    Intervals are skipped when the window RESET between the two samples (quota
    went up, so ``d_util <= 0``) or when nothing was spent, since neither prices
    anything. Returns None when no interval survives — the caller then falls back
    to the heuristic ceiling and the screen says the figure is an estimate.
    """
    tokens = 0.0
    util = 0.0
    for a, b in zip(samples, samples[1:]):
        left0 = a.session_left if session else a.week_left
        left1 = b.session_left if session else b.week_left
        if left0 is None or left1 is None:
            continue
        d_util = left0 - left1
        if d_util <= 0:
            continue
        d_tokens = (b.repo_tokens + b.other_tokens) - (a.repo_tokens + a.other_tokens)
        if d_tokens <= 0:  # also drops a counter reset
            continue
        tokens += d_tokens
        util += d_util
    if util <= 0 or tokens <= 0:
        return None
    return tokens / util


# MARK: - Distribution (the bell curve)


@dataclass(frozen=True)
class Bin:
    lower: float
    upper: float
    count: int


@dataclass(frozen=True)
class Distribution:
    """A histogram with a normal fitted over it and a confidence interval on the
    mean.

    The interval is on the MEAN, not on a single task: ``z * sd / sqrt(n)``. That
    is the question a budget is planned against ("what does a task cost on
    average, and how well do we know that"), and it is the one that keeps
    narrowing as the ledger fills. ``sd`` is the sample standard deviation (n-1),
    so a single observation reports 0 rather than pretending to a spread.
    """

    count: int = 0
    mean: float = 0.0
    sd: float = 0.0
    stderr: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    min: float = 0.0
    max: float = 0.0
    #: Printed beside the mean because this distribution is right-skewed in
    #: practice (most tasks are small, a few enormous), and a mean well above the
    #: median is the reader's cue that it is.
    median: float = 0.0
    bins: tuple[Bin, ...] = ()
    #: The fitted normal, sampled across the histogram's span and scaled to counts
    #: (density x n x bin width) so it can be drawn straight over the bars.
    curve: tuple[float, ...] = ()


def distribution(values: list[float], *, bin_count: int, z: float) -> Distribution:
    if not values:
        return Distribution()
    n = float(len(values))
    mean = sum(values) / n
    if len(values) > 1:
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    else:
        variance = 0.0
    sd = math.sqrt(variance)
    stderr = sd / math.sqrt(n) if len(values) > 1 else 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2

    # Bins run from 0, not from the smallest observation: this is a share of a
    # budget, so how close the mass sits to zero is the point of looking.
    hi = max(ordered[-1], 1e-9)
    width = hi / bin_count
    counts = [0] * bin_count
    for v in values:
        # The top edge belongs to the last bin; without the clamp the maximum
        # lands in bin `bin_count` and is dropped from its own histogram.
        idx = min(bin_count - 1, max(0, math.floor(v / width)))
        counts[idx] += 1
    bins = tuple(Bin(i * width, (i + 1) * width, counts[i]) for i in range(bin_count))

    curve: list[float] = []
    if sd > 0:
        points = bin_count * CURVE_RESOLUTION
        for i in range(points + 1):
            x = hi * i / points
            zx = (x - mean) / sd
            density = math.exp(-0.5 * zx * zx) / (sd * math.sqrt(2 * math.pi))
            curve.append(density * n * width)

    return Distribution(
        count=len(values), mean=mean, sd=sd, stderr=stderr,
        ci_low=mean - z * stderr, ci_high=mean + z * stderr,
        min=ordered[0], max=ordered[-1], median=median,
        bins=bins, curve=tuple(curve),
    )


# MARK: - Pending-over-time series


@dataclass(frozen=True)
class PendingPoint:
    at: float
    reviews: int
    conflicts: int


def pending_series(tasks: list[Task], *, now: float, days: float,
                   steps: int) -> list[PendingPoint]:
    """How much work was owed but unstarted, sampled at ``steps`` evenly spaced
    instants ending at ``now``. Split by duty, because the two answer different
    questions: reviews pile up when peers are waiting on you, conflict fixes when
    your own branches are rotting against main."""
    if steps <= 1 or days <= 0:
        return []
    span = days * 86400
    start = now - span
    points: list[PendingPoint] = []
    for i in range(steps):
        t = start + span * i / (steps - 1)
        reviews = conflicts = 0
        for task in tasks:
            if not task.pending(t):
                continue
            if task.duty == "conflicts":
                conflicts += 1
            else:
                reviews += 1
        points.append(PendingPoint(t, reviews, conflicts))
    return points


# MARK: - Rate-limit windows over time


@dataclass(frozen=True)
class QuotaPoint:
    at: float
    #: Percent of the 5-hour window left, or None when that reading is missing —
    #: the probe was offline, or Claude Code was logged out. None is NOT zero, and
    #: a chart must break its line rather than draw a plunge to the floor.
    session_pct: float | None
    week_pct: float | None


def quota_series(samples: list[Sample], *, now: float,
                 days: float) -> list[QuotaPoint]:
    """The quota readings inside the range, oldest first.

    Unlike :func:`pending_series` this is NOT resampled onto a fixed grid: these
    are measurements, taken every :data:`SAMPLE_INTERVAL_SECS`, and the 5-hour
    window's sawtooth is the shape worth seeing. Interpolating it onto evenly
    spaced instants would smooth away the resets that give it its meaning.
    """
    start = now - days * 86400
    return [
        QuotaPoint(
            at=s.at,
            session_pct=None if s.session_left is None else 100 * s.session_left,
            week_pct=None if s.week_left is None else 100 * s.week_left,
        )
        for s in samples
        if start <= s.at <= now
    ]


# MARK: - Token split


def token_split(samples: list[Sample]) -> tuple[float, float]:
    """Cumulative-counter deltas across the samples given, split monitored-repo vs
    everything else. A counter that went DOWN between two samples means the scanner's
    cursor file was lost and it restarted from zero, so that pair contributes nothing
    rather than a huge negative."""
    repo = other = 0.0
    for a, b in zip(samples, samples[1:]):
        if b.repo_tokens >= a.repo_tokens:
            repo += b.repo_tokens - a.repo_tokens
        if b.other_tokens >= a.other_tokens:
            other += b.other_tokens - a.other_tokens
    return repo, other


# MARK: - The whole screen, in one value


@dataclass(frozen=True)
class Summary:
    #: Tokens per 100% of the 5-hour session window, and of the 7-day week. None
    #: until enough samples exist to price the window, in which case every
    #: percentage is empty rather than guessed: Anthropic's limits are dynamic and
    #: account-specific, so a hardcoded ceiling would be a made-up number wearing a
    #: real one's clothes.
    session_limit_tokens: float | None = None
    week_limit_tokens: float | None = None

    per_task: Distribution = field(default_factory=Distribution)
    #: The same tasks against the 7-day window — one number, since the shape is
    #: the shape of ``per_task`` rescaled.
    per_task_week_mean: float = 0.0
    #: Mean RAW tokens per task. Independent of the quota probe, so it is what the
    #: screen shows while the window has no price yet — an unanchored number, but
    #: a measured one.
    per_task_tokens_mean: float = 0.0

    avg_run_secs: float = 0.0
    avg_wait_secs: float = 0.0
    run_samples: int = 0
    wait_samples: int = 0

    #: Every quota reading in the range, plus the latest of each window — what is
    #: left right now, which is the number the reader checks first.
    quota: tuple[QuotaPoint, ...] = ()
    session_left_pct: float | None = None
    week_left_pct: float | None = None

    pending: tuple[PendingPoint, ...] = ()
    pending_reviews_now: int = 0
    pending_conflicts_now: int = 0
    peak_reviews: int = 0
    peak_conflicts: int = 0

    repo_tokens: float = 0.0
    other_tokens: float = 0.0
    repo_share_pct: float = 0.0

    queued_count: int = 0
    started_count: int = 0
    done_count: int = 0
    remote_count: int = 0
    #: Finished tasks whose transcript could not be tied back to the run, so they
    #: carry no tokens and sit out the spread.
    unattributed_count: int = 0

    first_sample_at: float | None = None
    last_sample_at: float | None = None


def summarize(ledger: Ledger, *, now: float, days: float, steps: int,
              bin_count: int, z: float) -> Summary:
    """Reduce a folded ledger to everything the screen shows. ``now`` is injected
    so the two implementations — and the tests — agree on where the range ends.
    """
    start = now - days * 86400
    # The token counters are cumulative, so what the range spent is the rise since the
    # last reading taken BEFORE it opened. Starting from the first reading INSIDE it
    # drops everything spent between those two — a whole sample interval, which on a
    # bursty day is a sixth of what a 1-day range is being asked about.
    inside = [i for i, s in enumerate(ledger.samples) if start <= s.at <= now]
    samples = ledger.samples[max(0, inside[0] - 1):inside[-1] + 1] if inside else []

    # What a rate-limit window is worth in tokens is a property of the ACCOUNT, not
    # of the lookback the reader happens to have selected — so it is priced from
    # every sample in the ledger. That also means flipping to 7d doesn't blank the
    # percentages on a machine whose quota readings only began last week, and that
    # a short range borrows the precision of a long history.
    session_limit = calibrate(ledger.samples, session=True)
    week_limit = calibrate(ledger.samples, session=False)

    # A task belongs to the range by when it STARTED — that is the instant its
    # tokens were spent, and it keeps a task from moving between ranges as its
    # agent runs.
    in_range = [t for t in ledger.tasks
                if t.started_at is not None and start <= t.started_at <= now]
    local = [t for t in in_range if not t.remote]
    runs = [t.run_secs for t in local if t.run_secs is not None]
    waits = [t.wait_secs for t in in_range if t.wait_secs is not None]

    priced = [t.tokens for t in local if t.tokens is not None and t.tokens > 0]
    pct: list[float] = []
    if session_limit is not None and session_limit > 0:
        pct = [100 * tok / session_limit for tok in priced]
    week_mean = 0.0
    if week_limit is not None and week_limit > 0 and priced:
        week_mean = sum(100 * tok / week_limit for tok in priced) / len(priced)

    series = pending_series(ledger.tasks, now=now, days=days, steps=steps)
    quota = quota_series(ledger.samples, now=now, days=days)
    repo, other = token_split(samples)
    total = repo + other

    return Summary(
        session_limit_tokens=session_limit,
        week_limit_tokens=week_limit,
        per_task=distribution(pct, bin_count=bin_count, z=z),
        per_task_week_mean=week_mean,
        per_task_tokens_mean=sum(priced) / len(priced) if priced else 0.0,
        avg_run_secs=sum(runs) / len(runs) if runs else 0.0,
        avg_wait_secs=sum(waits) / len(waits) if waits else 0.0,
        run_samples=len(runs),
        wait_samples=len(waits),
        quota=tuple(quota),
        # The LAST reading that actually carried a value, not the last sample: a
        # probe that has been down for an hour must not blank a figure it measured
        # perfectly well an hour ago.
        session_left_pct=next((q.session_pct for q in reversed(quota)
                               if q.session_pct is not None), None),
        week_left_pct=next((q.week_pct for q in reversed(quota)
                            if q.week_pct is not None), None),
        pending=tuple(series),
        pending_reviews_now=series[-1].reviews if series else 0,
        pending_conflicts_now=series[-1].conflicts if series else 0,
        peak_reviews=max((p.reviews for p in series), default=0),
        peak_conflicts=max((p.conflicts for p in series), default=0),
        repo_tokens=repo,
        other_tokens=other,
        repo_share_pct=100 * repo / total if total > 0 else 0.0,
        queued_count=sum(1 for t in ledger.tasks
                         if t.queued_at is not None and start <= t.queued_at <= now),
        started_count=len(in_range),
        done_count=sum(1 for t in local if t.done_at is not None),
        remote_count=sum(1 for t in in_range if t.remote),
        unattributed_count=sum(1 for t in local
                               if t.done_at is not None and not (t.tokens or 0) > 0),
        first_sample_at=ledger.samples[0].at if ledger.samples else None,
        last_sample_at=ledger.samples[-1].at if ledger.samples else None,
    )


# MARK: - Formatting shared by both screens


def _round_half_away(value: float) -> int:
    """Swift's ``Double.rounded()`` — halves go away from zero. Python's built-in
    ``round`` is banker's rounding (``round(0.5) == 0``), so using it here would
    put the two platforms one second apart on every exact half and fail parity."""
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


def duration(secs: float, *, samples: int = 1) -> str:
    """``4m 20s`` / ``1h 05m`` / ``—`` for an empty sample. Both screens print
    durations in exactly one place each, and this is it, so they can't disagree
    about whether 90 minutes reads ``1h 30m`` or ``90m``."""
    if samples <= 0 or not math.isfinite(secs) or secs <= 0:
        return "—"
    total = _round_half_away(secs)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def percent(value: float) -> str:
    """A percentage at the precision it deserves: sub-1% figures keep two decimals
    (an auto-review really can cost 0.35% of a window), everything else one."""
    if not math.isfinite(value):
        return "—"
    if 0 < value < 1:
        return f"{value:.2f}%"
    return f"{value:.1f}%"


def tokens(value: float) -> str:
    """``1.2M`` / ``834k`` / ``512`` — token counts, which run to eight figures."""
    if not math.isfinite(value) or value <= 0:
        return "0"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(_round_half_away(value))


# MARK: - Parity payload


def _r(value: float) -> float:
    """Round to the parity precision (twin of ``TelemetryCommand.r``). Six places
    is far finer than anything rendered and far coarser than the last-bit
    disagreement two runtimes can have about ``exp``."""
    if not math.isfinite(value):
        return 0.0
    scale = 10.0 ** 6
    return _round_half_away(value * scale) / scale


def _opt(value: float | None):
    return None if value is None else _r(value)


def parity_payload(ledger: Ledger, summary: Summary) -> dict:
    """Everything ``diplomat-core telemetry`` prints, in the same shape — the
    subject of ``test_telemetry_parity.py``."""
    d = summary.per_task
    return {
        "tasks": [
            {
                "key": t.key, "duty": t.duty, "pr": t.pr,
                "queuedAt": _opt(t.queued_at), "startedAt": _opt(t.started_at),
                "doneAt": _opt(t.done_at), "clearedAt": _opt(t.cleared_at),
                "remote": t.remote, "tokens": _opt(t.tokens),
                "runSecs": _opt(t.run_secs), "waitSecs": _opt(t.wait_secs),
            }
            for t in ledger.tasks
        ],
        "sampleCount": len(ledger.samples),
        "sessionLimitTokens": _opt(summary.session_limit_tokens),
        "weekLimitTokens": _opt(summary.week_limit_tokens),
        "perTask": {
            "count": d.count,
            "mean": _r(d.mean), "sd": _r(d.sd), "stderr": _r(d.stderr),
            "ciLow": _r(d.ci_low), "ciHigh": _r(d.ci_high),
            "min": _r(d.min), "max": _r(d.max), "median": _r(d.median),
            "bins": [{"lower": _r(b.lower), "upper": _r(b.upper), "count": b.count}
                     for b in d.bins],
            "curve": [_r(v) for v in d.curve],
        },
        "perTaskWeekMean": _r(summary.per_task_week_mean),
        "perTaskTokensMean": _r(summary.per_task_tokens_mean),
        "avgRunSecs": _r(summary.avg_run_secs),
        "avgWaitSecs": _r(summary.avg_wait_secs),
        "runSamples": summary.run_samples,
        "waitSamples": summary.wait_samples,
        "quota": [{"at": _r(q.at), "sessionPct": _opt(q.session_pct),
                   "weekPct": _opt(q.week_pct)}
                  for q in summary.quota],
        "sessionLeftPct": _opt(summary.session_left_pct),
        "weekLeftPct": _opt(summary.week_left_pct),
        "pending": [{"at": _r(p.at), "reviews": p.reviews, "conflicts": p.conflicts}
                    for p in summary.pending],
        "pendingReviewsNow": summary.pending_reviews_now,
        "pendingConflictsNow": summary.pending_conflicts_now,
        "peakReviews": summary.peak_reviews,
        "peakConflicts": summary.peak_conflicts,
        "repoTokens": _r(summary.repo_tokens),
        "otherTokens": _r(summary.other_tokens),
        "repoSharePct": _r(summary.repo_share_pct),
        "queuedCount": summary.queued_count,
        "startedCount": summary.started_count,
        "doneCount": summary.done_count,
        "remoteCount": summary.remote_count,
        "unattributedCount": summary.unattributed_count,
        "firstSampleAt": _opt(summary.first_sample_at),
        "lastSampleAt": _opt(summary.last_sample_at),
        "format": {
            "run": duration(summary.avg_run_secs, samples=summary.run_samples),
            "wait": duration(summary.avg_wait_secs, samples=summary.wait_samples),
            "mean": percent(d.mean),
            "ciLow": percent(d.ci_low),
            "ciHigh": percent(d.ci_high),
            "weekMean": percent(summary.per_task_week_mean),
            "share": percent(summary.repo_share_pct),
            "perTaskTokens": tokens(summary.per_task_tokens_mean),
            "repoTokens": tokens(summary.repo_tokens),
            "otherTokens": tokens(summary.other_tokens),
        },
    }
