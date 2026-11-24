#!/usr/bin/env python3
"""Finds the satellite's RSC code from beacons it sent, so mrfsk.py can decode them with the Viterbi decoder.

The satellite codes its frames with IEEE 802.15.4g's recursive systematic code (RSC), whose exact form (which
generator is the feedback, which output goes first) mrfsk.py only guesses. A systematic code sends every data bit as
is, so the ground reads coded frames from those bits until the code is known (fec "systematic:<position>"), and the
soak keeps the coded bits of such frames in its run directory's coded.jsonl. Each of those frames passed its FCS, so
its data bits, tail and pad are known exactly: this tries every rate-1/2, constraint length 4 recursive code on them
and reports the one whose parity bits match.

    python soak/tools/fit_rsc.py soak/runs/<id>/coded.jsonl [more coded.jsonl ...]

It prints the Code(...) line to put in mrfsk.py as RSC (with confirmed=True), and what the frames' tail and pad bits
were, against what mrfsk.encode_frame() sends.
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hil", "lib"))

import mrfsk  # noqa: E402

# Feedback polynomials of degree 3 (bit k = D^k) and any feedforward polynomial with a constant term
FEEDBACKS = [0b1001, 0b1011, 0b1101, 0b1111]
FEEDFORWARDS = [g for g in range(1, 16) if g & 1]


def load(paths):
    """[(systematic position, coded bits)] from coded.jsonl files."""
    frames = []
    for path in paths:
        with open(path) as f:
            for line in f:
                record = json.loads(line)
                if not record.get("fec", "").startswith("systematic:"):
                    continue
                bits = np.unpackbits(np.frombuffer(bytes.fromhex(record["coded"]), dtype=np.uint8))[:record["n"]]
                frames.append((int(record["fec"].split(":")[1]), bits))
    return frames


def candidates(position):
    """Every recursive systematic code with the systematic bit at `position` of each pair."""
    for feedback in FEEDBACKS:
        for g in FEEDFORWARDS:
            outputs = (mrfsk.SYS, g) if position == 0 else (g, mrfsk.SYS)
            yield mrfsk.Code(f"RSC fb={feedback:#06b} g={g:#06b}", feedback, outputs)


def fit(frames):
    """[(match fraction, code)], best first: how many parity bits each candidate predicts, over all the frames."""
    if not frames:
        raise ValueError("no frames read from their systematic bits")
    positions = {p for p, _ in frames}
    if len(positions) != 1:
        raise ValueError(f"the frames disagree on which bit of a pair is systematic: {sorted(positions)}")
    position = positions.pop()
    scores = []
    for code in candidates(position):
        match = total = 0
        for _, bits in frames:
            info = bits[position::2]
            parity = bits[1 - position::2]
            predicted = code.encode(info)[1 - position::2]
            match += int(np.count_nonzero(predicted == parity))
            total += len(parity)
        scores.append((match / total, code))
    scores.sort(key=lambda s: -s[0])
    return scores


def tail_and_pad(frames):
    """For each frame: (tail bits sent, pad bits sent, pad bits mrfsk.pad_bits() expects). The tail bits are what the
    satellite's encoder fed in to end the frame; mrfsk.encode_frame() sends the ones that return it to state 0."""
    out = []
    for position, bits in frames:
        info = bits[position::2]
        length, _, _ = mrfsk.parse_phr(info[:16])
        end = 16 + 8 * length
        out.append((info[end:end + mrfsk.TAIL_BITS].tolist(), info[end + mrfsk.TAIL_BITS:].tolist(),
                    list(mrfsk.pad_bits(length))))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("coded", nargs="+", help="coded.jsonl files from soak runs")
    args = parser.parse_args()
    frames = load(args.coded)
    try:
        scores = fit(frames)
    except ValueError as e:
        print(f"Can't fit: {e}")
        return 1
    print(f"{len(frames)} frames, {sum(len(b) for _, b in frames) // 2} parity bits")
    for score, code in scores[:5]:
        print(f"  {score * 100:6.2f}%  {code.name}")
    best_score, best = scores[0]
    if best_score < 0.999:
        print("No candidate matches every parity bit: the code isn't in this family, or the frames have bit errors "
              "(look at the best ones' scores against the rest)")
        return 1
    print(f"\nPut in mrfsk.py:\nRSC = Code(\"RSC\", {best.feedback:#06b}, {tuple('SYS' if o == mrfsk.SYS else f'{o:#06b}' for o in best.outputs)})"
          .replace("'", ""))
    for tail, pad, want_pad in tail_and_pad(frames)[:3]:
        print(f"tail bits sent {tail}; pad bits sent {pad} (mrfsk expects {want_pad})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
