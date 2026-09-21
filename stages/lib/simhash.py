# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""64-bit simhash for near-duplicate detection (PLAN.md §7.2 dedup 判定级联 tier-2).

Fingerprint input is ``title_norm + ' ' + summary`` — see fingerprint_parts().
The summary MUST be in the hash: daily/periodical editions share a title
template that differs only in the date, so a title-only fingerprint lands
inside the near band and would falsely suppress a genuinely new edition
(verified in _selftest below).

Features (stdlib only):
  - zh  : char bigrams over each CJK run; a lone isolated char degrades to a
          unigram so it is not dropped.
  - en  : word shingles — unigrams plus adjacent-word bigrams, computed per
          contiguous [a-z0-9]+ run (a zh char or other boundary breaks a run).

Feature hash = first 8 bytes of sha1(feature) -> 64 votes of +-1, matching the
calibrated seed experiments/dedup-history/store.py (its ``[一-鿿]`` regex
matched single chars so its zh-bigram branch was dead code; this module emits
real bigrams — same hasher, same vote rule, so the calibrated thresholds stay
valid).

Public API:
  fingerprint(text) -> int          unsigned 64-bit
  fingerprint_parts(title, summary) fingerprint(title + ' ' + summary)
  hamming(a, b) -> int              64-bit popcount distance (sign-safe)
  to_signed64(x) / from_signed64(x) sqlite INTEGER column round-trip
  NEAR_DUP_HAMMING=4, GRAY_HAMMING=8   cascade thresholds from PLAN §7.2

Self-test:  uv run stages/lib/simhash.py   (or python3; exits nonzero on fail)
"""

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MASK64 = (1 << 64) - 1

# PLAN §7.2 判定级联阈值（已校准勿改）：
#   hamming <= NEAR_DUP_HAMMING            -> suppressed(dup_near)
#   cos >= 0.85 且 hamming <= GRAY_HAMMING  -> suppressed
NEAR_DUP_HAMMING = 4
GRAY_HAMMING = 8

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")


def features(text: str) -> list[str]:
    """Tokenize `text` into simhash features (zh char-bigram + en word shingle).

    zh runs -> char bigrams (lone char -> itself). en runs -> word unigrams
    plus adjacent-word bigrams. Public for tests/debug; order is irrelevant
    to the fingerprint.
    """
    feats: list[str] = []
    en_run: list[str] = []

    def _flush_en() -> None:
        feats.extend(en_run)
        feats.extend(f"{a} {b}" for a, b in zip(en_run, en_run[1:]))
        en_run.clear()

    for m in _TOKEN_RE.finditer((text or "").lower()):
        tok = m.group(0)
        if tok[0] >= "一":  # CJK run
            _flush_en()
            if len(tok) == 1:
                feats.append(tok)
            else:
                feats.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        else:
            en_run.append(tok)
    _flush_en()
    return feats


def fingerprint(text: str) -> int:
    """64-bit simhash of `text` (unsigned int in [0, 2**64))."""
    votes = [0] * 64
    for feat in features(text):
        h = int.from_bytes(hashlib.sha1(feat.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            votes[i] += 1 if (h >> i) & 1 else -1
    fp = 0
    for i in range(64):
        if votes[i] > 0:
            fp |= 1 << i
    return fp


def fingerprint_parts(title_norm: str, summary: str = "") -> int:
    """Fingerprint of the PLAN §7.2 hash input: ``title_norm + ' ' + summary``."""
    return fingerprint((title_norm or "") + " " + (summary or ""))


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit fingerprints.

    Both sides are masked to 64 bits first, so a signed sqlite INTEGER read
    back from history.db compares equal to the unsigned fingerprint.
    """
    return ((a & MASK64) ^ (b & MASK64)).bit_count()


def to_signed64(x: int) -> int:
    """unsigned 64 -> sqlite signed 64 (items.simhash column is INTEGER)."""
    x &= MASK64
    return x - (1 << 64) if x >= (1 << 63) else x


def from_signed64(x: int) -> int:
    """sqlite signed 64 -> unsigned fingerprint."""
    return x & MASK64


def _selftest() -> None:
    # --- identical / trivially-equivalent text -> distance 0 ---
    assert hamming(fingerprint("OpenAI 发布 GPT-6"), fingerprint("OpenAI 发布 GPT-6")) == 0
    # case + punctuation differences do not change the feature set
    assert hamming(fingerprint("GPT-6, released!"), fingerprint("gpt 6 released")) == 0
    assert fingerprint("") == 0
    assert hamming(0, 0) == 0
    # signed sqlite round-trip preserves the distance
    fp_a = fingerprint("round trip 测试")
    assert hamming(fp_a, to_signed64(fp_a)) == 0 and from_signed64(to_signed64(fp_a)) == fp_a

    # --- the date-sibling trap: titles alone DO collide-ish ---
    # Same daily-digest template, only the date differs. Title-only
    # fingerprints land inside the <=GRAY_HAMMING near band — hashing title
    # alone would mark two different editions as the same story.
    title_a = "AI 早报：今日人工智能前沿资讯一网打尽 2026-09-21"
    title_b = "AI 早报：今日人工智能前沿资讯一网打尽 2026-09-20"
    sum_a = "OpenAI 发布 GPT-6，支持百万 token 上下文；谷歌 Gemini 3 同步上线多模态推理。"
    sum_b = "英伟达发布新一代 Rubin GPU，推理性能翻倍；台积电 2nm 量产进度提前。"
    d_title_only = hamming(fingerprint(title_a), fingerprint(title_b))
    d_combined = hamming(fingerprint_parts(title_a, sum_a), fingerprint_parts(title_b, sum_b))
    assert d_title_only <= GRAY_HAMMING, d_title_only  # collide-ish: 落入近邻带
    assert d_combined > NEAR_DUP_HAMMING, d_combined   # summary 进 hash 才分得开
    assert d_combined > d_title_only

    # --- sanity of the band edges ---
    # near-verbatim repost (one word swapped) stays in the near band
    x1 = "OpenAI 发布 GPT-6 " + sum_a
    x2 = "OpenAI 正式推出 GPT-6 " + sum_a.replace("发布", "推出")
    assert hamming(fingerprint(x1), fingerprint(x2)) <= 12
    # unrelated stories are far away (~random)
    assert hamming(fingerprint_parts("OpenAI 发布 GPT-6", sum_a),
                   fingerprint_parts("英伟达发布 Rubin GPU", sum_b)) >= 24

    print(f"simhash selftest ok: d_title_only={d_title_only} d_combined={d_combined}")


if __name__ == "__main__":
    _selftest()
