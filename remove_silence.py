#!/usr/bin/env python3
"""
Remove silence, breaths, and gaps from MP4 videos.

Usage:
    python remove_silence.py input.mp4
    python remove_silence.py input.mp4 -o output.mp4
    python remove_silence.py input.mp4 --threshold -30dB --min-silence 0.3
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile


def detect_silences(input_path, threshold="-30dB", min_silence_duration=0.3):
    """Use ffmpeg silencedetect to find silent segments."""
    cmd = [
        "ffprobe",
        "-f", "lavfi",
        "-i", f"amovie={input_path},silencedetect=noise={threshold}:d={min_silence_duration}",
        "-show_entries", "frame_tags=lavfi.silence_start,lavfi.silence_end",
        "-of", "json",
        "-v", "quiet",
    ]

    # Fallback: use ffmpeg stderr parsing (more reliable across versions)
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-af", f"silencedetect=noise={threshold}:d={min_silence_duration}",
        "-f", "null", "-",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    # Parse silence_start and silence_end from ffmpeg output
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", stderr)]

    # If there's an unmatched start (silence goes to end of file), pair it with duration
    silences = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else None
        silences.append((start, end))

    return silences


def get_duration(input_path):
    """Get total duration of the input file."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json", input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def compute_segments(silences, total_duration, padding=0.05):
    """
    Given silence intervals, compute the non-silent segments to keep.
    Adds a small padding around cuts to avoid clipping words.
    """
    segments = []
    current = 0.0

    for silence_start, silence_end in silences:
        # Segment of audio/video to keep: from current position to silence start
        seg_start = current
        seg_end = silence_start + padding  # keep a tiny bit into the silence

        if seg_end > seg_start + 0.05:  # only keep segments longer than 50ms
            segments.append((max(0, seg_start), min(seg_end, total_duration)))

        if silence_end is not None:
            current = silence_end - padding  # start a tiny bit before speech resumes
        else:
            current = total_duration  # silence goes to end

    # Keep any remaining content after the last silence
    if current < total_duration - 0.05:
        segments.append((max(0, current), total_duration))

    return segments


def build_and_run_ffmpeg(input_path, output_path, segments):
    """Use ffmpeg filter_complex with trim/atrim for frame-accurate cuts."""
    if not segments:
        print("Error: No non-silent segments found. Try adjusting --threshold or --min-silence.")
        sys.exit(1)

    n = len(segments)
    print(f"Found {n} segments to keep")

    # Build a single filter_complex that trims and concatenates all segments.
    # This re-encodes but gives frame-accurate cuts with no duplication.
    video_filters = []
    audio_filters = []
    concat_inputs = []

    for i, (start, end) in enumerate(segments):
        video_filters.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]"
        )
        audio_filters.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")

    concat_str = "".join(concat_inputs)
    filter_parts = video_filters + audio_filters
    filter_parts.append(f"{concat_str}concat=n={n}:v=1:a=1[outv][outa]")

    filter_complex = ";\n".join(filter_parts)

    cmd = [
        "ffmpeg", "-v", "warning",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", output_path,
    ]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Remove silence, breaths, and gaps from MP4 videos."
    )
    parser.add_argument("input", help="Input MP4 file")
    parser.add_argument("-o", "--output", help="Output file path (default: input_cleaned.mp4)")
    parser.add_argument(
        "--threshold", default="-30dB",
        help="Silence threshold in dB (default: -30dB). Lower = only detect true silence. "
             "Higher (e.g. -20dB) = also catch breaths."
    )
    parser.add_argument(
        "--min-silence", type=float, default=0.5,
        help="Minimum silence duration in seconds to remove (default: 0.5)"
    )
    parser.add_argument(
        "--padding", type=float, default=0.2,
        help="Padding in seconds to keep around speech boundaries (default: 0.2)"
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: File not found: {args.input}")
        sys.exit(1)

    output = args.output
    if not output:
        base, ext = os.path.splitext(args.input)
        output = f"{base}_cleaned{ext}"

    print(f"Input:  {args.input}")
    print(f"Output: {output}")
    print(f"Threshold: {args.threshold}, Min silence: {args.min_silence}s, Padding: {args.padding}s")
    print()

    # Step 1: Get duration
    total_duration = get_duration(args.input)
    print(f"Total duration: {total_duration:.2f}s")

    # Step 2: Detect silences
    print("Detecting silence...")
    silences = detect_silences(args.input, args.threshold, args.min_silence)
    print(f"Found {len(silences)} silent segments")

    if not silences:
        print("No silence detected. Output will be the same as input.")
        print("Try raising --threshold (e.g. -20dB) to catch more quiet sections.")
        return

    # Step 3: Compute non-silent segments
    segments = compute_segments(silences, total_duration, args.padding)

    kept_duration = sum(end - start for start, end in segments)
    removed = total_duration - kept_duration
    print(f"Keeping {kept_duration:.2f}s, removing {removed:.2f}s ({removed/total_duration*100:.1f}%)")
    print()

    # Step 4: Build output
    print("Building output...")
    build_and_run_ffmpeg(args.input, output, segments)

    output_size = os.path.getsize(output) / (1024 * 1024)
    print(f"\nDone! Output: {output} ({output_size:.1f} MB)")


if __name__ == "__main__":
    main()
