#!/usr/bin/env python3
"""animate_frames.py — Turbo LoRA / MiniMax T2V frame animation for YouTube Shorts.

Integrates the workflow from the YouTube video:
  "8GB VRAM으로 MiniMax 애니 만들기 | Alibaba Turbo LoRA 워크플로우"

Each static PIL frame is sent to MiniMax H3 T2V API → short animation clip.
Supports both per-segment full animation and hook-only (first line) animation.

Usage:
    # Full animation (each segment → T2V)
    python -m freetoken.animate_frames --mode full --api-key $MINIMAX_H3_API_KEY

    # Hook-only animation (only first line animated)
    python -m freetoken.animate_frames --mode hook --api-key $MINIMAX_H3_API_KEY

    # Dry run (estimate cost/time)
    python -m freetoken.animate_frames --mode full --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

# ── Frame rendering (lazy import to avoid circular imports) ──
_RENDER_FRAME = None
_OUTPUT_DIR = None

def _ensure_render_frame():
    global _RENDER_FRAME, _OUTPUT_DIR
    if _RENDER_FRAME is None:
        try:
            from main import OUTPUT_DIR, _render_frame as rf
            _OUTPUT_DIR = OUTPUT_DIR
            _RENDER_FRAME = rf
        except ImportError:
            # Fallback: simple render
            from PIL import Image, ImageDraw, ImageFont
            def rf(line: str, fp: str, vw: int = 1080, vh: int = 1920,
                   idx: int = 0, tot: int = 1) -> None:
                img = Image.new("RGB", (vw, vh), (13, 16, 42))
                draw = ImageDraw.Draw(img)
                tc = (255, 209, 102) if idx == 0 else (255, 255, 255)
                font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
                if not os.path.exists(font_path):
                    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                for fs in range(72, 36, -4):
                    font = ImageFont.truetype(font_path, fs)
                    if draw.textlength(line, font=font) < vw * 0.88:
                        break
                tw = draw.textlength(line, font=font)
                draw.text(((vw-tw)//2, vh//2), line, font=font, fill=tc)
                img.save(fp)
            _RENDER_FRAME = rf
    return _RENDER_FRAME, _OUTPUT_DIR

# ── Config ────────────────────────────────────────────────────────────────────
# MiniMax H3 T2V settings
# Local CogVideoX-2b T2V server (default)
T2V_BASE_URL = os.getenv("MINIMAX_H3_BASE_URL", "http://localhost:8084")
T2V_MODEL = os.getenv("H3_T2V_MODEL", "cogvideox-2b")
T2V_API_KEY = os.getenv("MINIMAX_H3_API_KEY", "")

# Animation settings
CLIP_DURATION = 3  # seconds per animated segment (max 30 for H3)
ANIMATION_STRENGTH = 0.7  # motion intensity: 0.0=no motion, 1.0=full motion
HOOK_ONLY = False  # only animate the first (hook) line

# Cost control
MAX_SEGMENTS = 12  # max segments to animate (budget)
COST_PER_VIDEO = 0.05  # USD per H3 T2V call (approx)


def get_api_key() -> str | None:
    """Get the MiniMax API key from env or prompt. Returns None for local server."""
    if T2V_API_KEY:
        return T2V_API_KEY
    # Try reading from a file
    key_path = os.path.join(os.path.expanduser("~"), ".config", "minimax_api_key.txt")
    if os.path.exists(key_path):
        return os.path.join(os.path.expanduser("~"), ".config", "minimax_api_key.txt")
    # Local server doesn't require a key
    return None


def frame_to_animation_prompt(frame_path: str, line_text: str, is_hook: bool = False) -> str:
    """Generate an animation prompt from a frame and its line text.

    The prompt tells the T2V model how to animate this static frame.
    Uses the YouTube video's approach: describe the visual + motion direction.
    """
    motion_desc = []
    if is_hook:
        # Hook line gets extra emphasis — subtle camera push + text glow
        motion_desc.extend([
            "subtle camera push-in (zoom 1.0 to 1.05)",
            "text area has a gentle golden glow pulse",
            "background gradient slowly shifts hue",
        ])
    else:
        # Non-hook: gentle ambient motion
        motion_desc.extend([
            "subtle ambient motion — background particles drift slowly",
            "text remains sharp, background moves minimally",
        ])

    prompt = f"""A vertical 9:16 video (1080x1920).
The scene has a gradient background going from dark navy/purple to deep blue or magenta.
White text with a golden accent on the screen.
{', '.join(motion_desc)}.

The text content is: "{line_text[:80]}"

The animation should feel premium and smooth — like a high-quality mobile app animation.
Motion should be subtle and professional, not flashy or chaotic.
"""
    return prompt


def generate_segment_animation(
    frame_path: str,
    line_text: str,
    is_hook: bool = False,
    api_key: str | None = None,
    duration: int = CLIP_DURATION,
) -> str | None:
    """Generate an animated clip from a static frame using MiniMax H3 T2V.

    Args:
        frame_path: Path to the PIL-rendered PNG frame.
        line_text: The script line text (for prompt generation).
        is_hook: Whether this is the first (hook) line.
        api_key: MiniMax API key.
        duration: Video duration in seconds.

    Returns:
        Path to the generated MP4 clip.
    """
    import requests
    from openai import OpenAI

    key = api_key or get_api_key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    prompt = frame_to_animation_prompt(frame_path, line_text, is_hook)

    client = OpenAI(base_url=T2V_BASE_URL, api_key=key if key else "sk-xxx")

    # Upload frame to MiniMax (if needed) or use base64
    # For H3 T2V, we can pass the frame as a base64 image
    with open(frame_path, "rb") as f:
        frame_b64 = f.read().hex()

    # Create the video generation request
    response = client.videos.create(
        model=T2V_MODEL,
        prompt=prompt,
    )

    video_id = response.data.id
    print(f"  [T2V] Starting animation for segment — ID: {video_id}")

    # Poll for completion
    status_url = f"{T2V_BASE_URL}/videos/{video_id}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    for attempt in range(120):  # Max 10 minutes
        try:
            status_resp = requests.get(status_url, headers=headers, timeout=30)
            if status_resp.status_code != 200:
                time.sleep(5)
                continue

            status_data = status_resp.json()
            status = status_data.get("data", {}).get("status", "unknown")

            if status == "completed":
                download_url = status_data["data"]["downloads"]["url"]
                break
            elif status == "failed":
                raise RuntimeError(f"T2V generation failed: {status_data.get('data', {}).get('message', 'unknown')}")

            if attempt % 6 == 0:
                print(f"  [T2V] Polling... status: {status} (attempt {attempt + 1}/120)")

            time.sleep(5)
        except Exception as e:
            print(f"  [T2V] Poll error: {e}")
            time.sleep(5)
    else:
        raise RuntimeError("T2V video generation timed out after 10 minutes.")

    # Download video
    print(f"  [T2V] Downloading animation clip...")
    r = requests.get(download_url, timeout=120)
    r.raise_for_status()

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(r.content)
    tmp.close()
    return tmp.name


def animate_all_segments(
    script: str,
    frames_dir: str,
    output_dir: str,
    mode: str = "full",
    api_key: str | None = None,
    dry_run: bool = False,
) -> str:
    """Animate all segments and produce a final video.

    Args:
        script: Script text (each line = one segment).
        frames_dir: Directory containing frame_XXXX.png files.
        output_dir: Output directory for clips and final video.
        mode: "full" (every segment) or "hook" (first line only).
        api_key: MiniMax API key override.
        dry_run: If True, only estimate cost/time without generating.

    Returns:
        Path to the final MP4 video.
    """
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    if not lines:
        raise ValueError("Empty script — cannot animate")

    # Limit segments for cost control
    lines = lines[:MAX_SEGMENTS]
    num_animated = len(lines) if mode == "full" else 1

    if dry_run:
        estimated_cost = num_animated * COST_PER_VIDEO
        estimated_time = num_animated * 45  # seconds (avg T2V generation time)
        print(f"  [DRY RUN] Estimated {num_animated} animations:")
        print(f"    Cost: ${estimated_cost:.2f} USD")
        print(f"    Time: ~{estimated_time // 60}m {estimated_time % 60}s")
        for i, line in enumerate(lines):
            is_hook = (i == 0) and (mode == "hook")
            print(f"    [{i+1}] {'[HOOK]' if is_hook else '     '} {line[:60]}")
        return ""

    os.makedirs(output_dir, exist_ok=True)
    clips_dir = os.path.join(output_dir, "animated_clips")
    os.makedirs(clips_dir, exist_ok=True)

    # Process each segment
    clip_paths = []
    for i, line in enumerate(lines):
        is_hook = (i == 0) and (mode == "hook")

        # Check if mode allows this segment
        if mode == "hook" and i > 0:
            # Use original frame as still image for non-hook segments
            frame_path = os.path.join(frames_dir, f"frame_{i:04d}.png")
            if os.path.exists(frame_path):
                # Create a "still" clip: loop the frame for CLIP_DURATION
                still_clip = os.path.join(clips_dir, f"seg_{i:04d}_still.mp4")
                subprocess.run(
                    ["ffmpeg", "-y", "-loop", "1", "-i", frame_path,
                     "-t", str(CLIP_DURATION),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
                     "-f", "null", "/dev/null"],
                    check=True, capture_output=True,
                )
                # Actually render the still clip properly
                subprocess.run(
                    ["ffmpeg", "-y", "-loop", "1", "-i", frame_path,
                     "-vf", "setpts=0.5*PTS",
                     "-t", str(CLIP_DURATION),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
                     still_clip],
                    check=True, capture_output=True,
                )
                clip_paths.append(still_clip)
            else:
                clip_paths.append(None)
            continue

        frame_path = os.path.join(frames_dir, f"frame_{i:04d}.png")
        if not os.path.exists(frame_path):
            print(f"  [WARN] Frame {i+1} not found, skipping animation")
            clip_paths.append(None)
            continue

        print(f"\n  [{i+1}/{len(lines)}] Animating segment {i+1} "
              f"{'[HOOK]' if is_hook else ''}: {line[:50]}...")

        try:
            clip_path = generate_segment_animation(
                frame_path=frame_path,
                line_text=line,
                is_hook=is_hook,
                api_key=api_key,
                duration=CLIP_DURATION,
            )
            # Rename to predictable path
            final_clip = os.path.join(clips_dir, f"seg_{i:04d}_anim.mp4")
            os.rename(clip_path, final_clip)
            clip_paths.append(final_clip)
            print(f"  [T2V] ✓ Animation saved to {final_clip}")
        except Exception as e:
            print(f"  [T2V] ✗ Failed for segment {i+1}: {e}")
            clip_paths.append(None)

    # Concat all clips
    print(f"\n  [MERGE] Combining {len(clip_paths)} clips into final video...")
    final_path = os.path.join(output_dir, "final_animated.mp4")

    clip_files = [p for p in clip_paths if p and os.path.exists(p)]
    if not clip_files:
        raise RuntimeError("No valid clips to merge")

    list_file = os.path.join(output_dir, "clips_list.txt")
    with open(list_file, "w") as f:
        for fp in clip_files:
            f.write(f"file '{fp}'\n")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_file,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart",
         final_path],
        check=True, capture_output=True,
    )

    # Cleanup
    for d in (clips_dir,):
        if os.path.isdir(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)
    if os.path.exists(list_file):
        os.remove(list_file)

    return final_path


def build_animated_video(
    script: str,
    frames_dir: str,
    output_path: str,
    mode: str = "hook",
    video_gen_model: str = "h3",
) -> str:
    """Build video with optional frame animation (integrated into main.py build_video).

    This is the function that main.py will call when --video-anim is specified.

    Args:
        script: Script lines.
        frames_dir: Directory with rendered frames.
        output_path: Where to save the final MP4.
        mode: "full" or "hook".
        video_gen_model: "h3" for MiniMax T2V.

    Returns:
        Path to final MP4.
    """
    _render_frame, OUTPUT_DIR = _ensure_render_frame()
    output_dir = os.path.dirname(output_path) or "."
    OUTPUT_DIR = OUTPUT_DIR or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

    # ── Step 1: Render frames first ──
    frames_dir = os.path.join(OUTPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    lines = [l.strip() for l in script.split("\n") if l.strip()]
    video_w, video_h = 1080, 1920

    for i, line in enumerate(lines):
        frame_path = os.path.join(frames_dir, f"frame_{i:04d}.png")
        _render_frame(line, frame_path, video_w, video_h, index=i, total=len(lines))

    # ── Step 2: Animate segments ──
    result = animate_all_segments(
        script=script,
        frames_dir=frames_dir,
        output_dir=output_dir,
        mode=mode,
        dry_run=False,
    )

    if result and os.path.exists(result):
        # Move to output_path
        if result != output_path:
            os.rename(result, output_path)
        return output_path

    raise RuntimeError("Animation failed — no output video")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Animate YouTube Shorts frames using MiniMax T2V (Turbo LoRA workflow)"
    )
    parser.add_argument(
        "--mode",
        choices=["full", "hook"],
        default="hook",
        help="Animation mode: 'full' (all segments) or 'hook' (first line only, default). Default: hook",
    )
    parser.add_argument("--api-key", default=None, help="MiniMax H3 API key")
    parser.add_argument("--dry-run", action="store_true", help="Estimate cost/time without generating")
    parser.add_argument("--script", default=None, help="Script file path (JSON with 'lines' array)")
    parser.add_argument("--frames-dir", default=None, help="Directory containing frame_XXXX.png files")
    parser.add_argument("--output-dir", default="output", help="Output directory")

    args = parser.parse_args()

    if not args.dry_run:
        key = args.api_key or get_api_key()
    else:
        key = "sk-dry-run"

    # If script + frames provided, run full pipeline
    if args.script and args.frames_dir:
        with open(args.script) as f:
            data = json.load(f)
        script = "\n".join(data.get("lines", []))

        result = animate_all_segments(
            script=script,
            frames_dir=args.frames_dir,
            output_dir=args.output_dir,
            mode=args.mode,
            api_key=key,
            dry_run=args.dry_run,
        )

        if result and not args.dry_run:
            print(f"\n  [SUCCESS] Final video: {result}")
    else:
        # Dry run with sample
        sample_script = "Your phone's notification sound was engineered by slot machine designers.\nThe free app you love checks your location 47 times an hour.\nYou don't need another productivity app.\nOne boring alarm that kills all notifications for two hours.\nYour brain has no volume knob for notifications.\nThe loudest voice wins — right now that's your phone, not you.\nYou aren't distracted; you're being hunted by nine billion dollars of attention engineering.\nYour phone battery is the only relationship that honestly reports time left."

        result = animate_all_segments(
            script=sample_script,
            frames_dir="/tmp/dummy_frames",
            output_dir=args.output_dir,
            mode=args.mode,
            api_key=key,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
