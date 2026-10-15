#!/usr/bin/env python3
"""upload_youtube.py — Upload a video to YouTube (or simulate if no API keys)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

import env  # loads .env (token paths, endpoint overrides)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
FAILED_FILE = os.path.join(OUTPUT_DIR, "FAILED_UPLOAD.txt")


def load_meta() -> dict:
    """Load metadata from meta.json if it exists."""
    meta_path = os.path.join(OUTPUT_DIR, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            return json.load(f)
    return {}


def load_tokens():
    """Load OAuth tokens. Tries known locations in order:
    1. ./tokens.json, ./token.json (repo-local)

    Returns a list of (data, path) for every candidate file that contains a
    refresh token — callers should try refreshing with each in order, because
    multiple files can coexist and only one may still be valid.
    """
    candidates = [
        os.path.join(SCRIPT_DIR, "tokens.json"),
        os.path.join(SCRIPT_DIR, "token.json"),
    ]
    found = []
    seen_paths = set()
    for tok_path in candidates:
        real = os.path.realpath(tok_path)
        if not os.path.exists(tok_path) or real in seen_paths:
            continue
        try:
            with open(tok_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("refresh_token") or data.get("token"):
            seen_paths.add(real)
            found.append((data, tok_path))
    return found


def refresh_token(refresh_token, client_id, client_secret="") -> dict:
    """Get fresh access token from refresh token."""
    url = "https://oauth2.googleapis.com/token"
    data = f"client_id={client_id}&refresh_token={refresh_token}&grant_type=refresh_token"
    if client_secret:
        data += f"&client_secret={client_secret}"
    req = Request(url, data=data.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read())


def upload_api(video_path: str, title: str, description: str,
               tags: list[str], status: str) -> str:
    """Upload via youtube-upload CLI (gclient tool)."""
    cmd = [
        "youtube-upload",
        "--file", video_path,
        "--title", title,
        "--description", description,
        "--tags", ",".join(tags),
        "--privacy-status", status,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"youtube-upload failed: {r.stderr}")
    # youtube-upload prints a URL on success
    return r.stdout.strip().split()[-1] if r.stdout.strip() else "unknown"


def upload_oauth(video_path: str, title: str, description: str,
                 tags: list[str], status: str) -> str:
    """Upload via YouTube Data API v3 using OAuth2 tokens.
    Uses two-step process: 1) insert video with metadata, 2) upload video binary.

    Multiple token files can coexist (tokens.json vs token.json) and only one
    may hold a valid refresh token — try refreshing with each candidate.
    """
    candidates = load_tokens()
    if not candidates:
        raise RuntimeError("No OAuth tokens found (checked ./{tokens,token}.json)")

    access_token = None
    used_tok_path = None
    for tokens, tok_path in candidates:
        refresh_t = tokens.get("refresh_token", "")
        client_id = tokens.get("client_id", "")
        if not (refresh_t and client_id):
            continue
        try:
            new_tokens = refresh_token(refresh_t, client_id, tokens.get("client_secret", ""))
            access_token = new_tokens.get("access_token")
            if access_token:
                used_tok_path = tok_path
                # Write the fresh access token back to the file that worked
                tokens["token"] = access_token
                tokens["access_token"] = access_token
                with open(tok_path, "w") as f:
                    json.dump(tokens, f, indent=2)
                break
        except Exception as e:
            print(f"  [WARN] Token refresh failed for {tok_path}: {e}")

    if not access_token:
        # Last resort: use a non-refreshed access token from any file
        for tokens, tok_path in candidates:
            if tokens.get("token") or tokens.get("access_token"):
                access_token = tokens.get("token") or tokens.get("access_token")
                used_tok_path = tok_path
                break

    if not access_token:
        raise RuntimeError("No valid access token (all refresh attempts failed)")

    # Prepare video data
    with open(video_path, "rb") as f:
        video_data = f.read()

    # Step 1: Insert video with snippet only (separate from status)
    snippet = {
        "title": title,
        "description": description,
        "tags": tags,
    }
    body = json.dumps({"snippet": snippet}).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    req = Request("https://www.googleapis.com/upload/youtube/v3/videos?part=snippet&uploadType=resumable",
                  data=body, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=30)
        upload_url = resp.headers.get("Location")
    except Exception as e:
        # Read error details from response body
        code = getattr(e, 'code', '?')
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')
        except:
            pass
        msg = str(e) + (f' [body: {err_body[:500]}]' if err_body else '')
        print(f"  [DEBUG] Insert error code: {code}")
        print(f"  [DEBUG] Insert error message: {msg[:500]}")
        raise
    if not upload_url:
        raise RuntimeError(f"No upload URL from insert: {resp.headers}")

    # Step 2: Upload video binary to resumable upload URL
    try:
        upload_req = Request(upload_url, data=video_data,
                             headers={"Content-Type": "video/mp4", "Content-Length": str(len(video_data))},
                             method="PUT")
        upload_resp = urlopen(upload_req, timeout=120)
        final = json.loads(upload_resp.read())
    except Exception as e:
        # Read error details
        try:
            err_body = json.loads(str(e))
            print(f"  [DEBUG] Upload error: {err_body}")
        except:
            print(f"  [DEBUG] Upload error: {e}")
        raise
    video_id = final.get("id")
    if not video_id:
        raise RuntimeError(f"No video ID in response: {final}")

    # Step 3: Update video status (privacy) via PATCH API
    status_patch = json.dumps({"status": {"privacyStatus": status}}).encode()
    patch_req = Request(
        f"https://www.googleapis.com/youtube/v3/videos?part=status",
        data=status_patch,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="PATCH"
    )
    try:
        patch_resp = urlopen(patch_req, timeout=15)
    except Exception:
        pass  # status update is optional, video still exists

    print(f"  Video ID: {video_id}")
    return f"https://youtu.be/{video_id}"


def upload_yt_dlp(video_path: str, title: str, description: str,
                 tags: list[str], status: str) -> str:
    """Upload via yt-dlp with YouTube auth (uses cookies.json).

    NOTE: current yt-dlp builds do not support an --upload flag; this path is
    kept for forward compatibility but will raise until they do.
    """
    help_r = subprocess.run(["yt-dlp", "--help"], capture_output=True, text=True)
    if "--upload" not in (help_r.stdout or ""):
        raise RuntimeError("yt-dlp build has no --upload option")

    cmd = [
        "yt-dlp", "--upload", video_path,
        "--title", title,
        "--description", description,
        "--tags", ",".join(tags),
    ]
    if os.path.exists(os.path.join(SCRIPT_DIR, "cookies.json")):
        cmd.extend(["--cookies", "cookies.json"])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp upload failed: {r.stderr}")
    return "uploaded"


def delete_video(video_id: str) -> bool:
    """Delete a channel video via YouTube Data API v3 (needs the same OAuth token as upload)."""
    for tokens, tok_path in load_tokens():
        refresh_t = tokens.get("refresh_token", "")
        client_id = tokens.get("client_id", "")
        if not (refresh_t and client_id):
            continue
        try:
            new_tokens = refresh_token(refresh_t, client_id, tokens.get("client_secret", ""))
            access_token = new_tokens.get("access_token")
            if not access_token:
                continue
            tokens["token"] = access_token
            tokens["access_token"] = access_token
            with open(tok_path, "w") as f:
                json.dump(tokens, f, indent=2)
            req = Request(
                f"https://www.googleapis.com/youtube/v3/videos?videoId={video_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                method="DELETE",
            )
            urlopen(req, timeout=30)
            return True
        except Exception as e:
            print(f"  [WARN] delete failed via {tok_path}: {e}")
    return False


def record_failure(video: str, reason: str):
    """Log a failed upload to FAILED_UPLOAD.txt."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(FAILED_FILE, "a") as f:
        f.write(f"{datetime.now().isoformat()} - {video}: {reason}\n")


def run_upload(video_path: str, title: str, description: str,
               tags: list[str], status: str) -> str | None:
    """Try all upload backends in order. Returns URL on success, None on failure."""
    uploaded_url = None
    last_error = None

    has_youtube_upload = subprocess.run(["which", "youtube-upload"],
                                        capture_output=True).returncode == 0
    has_yt_dlp = subprocess.run(["which", "yt-dlp"],
                                 capture_output=True).returncode == 0
    has_oauth = len(load_tokens()) > 0

    # OAuth (README step 2, token.json) is the documented path, so try it first.
    if has_oauth:
        try:
            uploaded_url = upload_oauth(video_path, title, description, tags, status)
            print(f"[OK] Uploaded via OAuth API: {uploaded_url}")
        except Exception as e:
            last_error = str(e)
            print(f"[WARN] OAuth upload failed: {e}")

    if uploaded_url is None and has_youtube_upload:
        try:
            uploaded_url = upload_api(video_path, title, description, tags, status)
            print(f"[OK] Uploaded via youtube-upload: {uploaded_url}")
        except Exception as e:
            last_error = str(e)
            print(f"[WARN] youtube-upload failed: {e}")

    # yt-dlp upload is disabled in most current builds — last resort only.
    if uploaded_url is None and has_yt_dlp:
        try:
            uploaded_url = upload_yt_dlp(video_path, title, description, tags, status)
            print("[OK] Uploaded via yt-dlp")
        except Exception as e:
            last_error = str(e)
            print(f"[WARN] yt-dlp failed: {e}")

    if uploaded_url is None:
        # Simulated upload — save metadata for later manual upload
        sim_path = os.path.join(OUTPUT_DIR, "upload_pending.json")
        with open(sim_path, "w") as f:
            json.dump({
                "video": video_path,
                "title": title,
                "description": description,
                "tags": tags,
                "status": status,
                "uploaded_at": datetime.now().isoformat(),
                "last_error": last_error,
            }, f, indent=2)
        print(f"[SIM] Nothing uploaded. Metadata saved to {sim_path}")
        if not has_oauth:
            print("  No OAuth token found — finish README step 2 (YouTube OAuth) and put "
                  "token.json in this directory, then re-run.")
        print(f"  Last error: {last_error}")
    return uploaded_url


def main():
    parser = argparse.ArgumentParser(description="Upload YouTube video")
    parser.add_argument("--video", required=False, help="Path to MP4")
    parser.add_argument("--batch", action="store_true",
                        help="Upload output/final.mp4 + meta.json (cron mode). "
                             "Archives uploaded files so the same video is not re-uploaded.")
    parser.add_argument("--title", default=None, help="Upload title")
    parser.add_argument("--description", default=None, help="Upload description")
    parser.add_argument("--status", default="public",
                        choices=["public", "unlisted", "private"])
    parser.add_argument("--tags", nargs="+", default=[], help="Video tags")
    parser.add_argument("--review", action="store_true",
                        help="Accept flag for cron compatibility (pre-upload review is "
                             "handled by main.py's evaluate_script stage)")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would upload")
    parser.add_argument("--delete", default=None, metavar="VIDEO_ID",
                        help="Delete a video by ID (e.g. --delete GktGttHqCrg) and exit")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if args.delete:
        vid = args.delete
        if "/" in vid:
            vid = vid.rstrip("/").split("/")[-1]
        print(f"Deleting video: {vid}")
        if delete_video(vid):
            print(f"[OK] Deleted {vid}")
        else:
            print(f"[FAIL] could not delete {vid}")
            sys.exit(1)
        return

    if args.batch:
        video_path = os.path.join(OUTPUT_DIR, "final.mp4")
        meta = load_meta()
        if not os.path.exists(video_path) or not meta.get("title"):
            print("[BATCH] Nothing to upload (no final.mp4 / meta.json). Exiting.")
            return
        title = args.title or meta.get("title", "untitled")
        description = args.description or meta.get("description", "")
        tags = args.tags or meta.get("tags", [])
        # batch (cron) mode: the pipeline's own status decision (meta.json) wins over
        # the CLI default, so a --status private generation is never force-published.
        status = meta.get("status") or args.status
    else:
        if not args.video:
            parser.error("--video is required (or use --batch)")
        video_path = args.video
        if not os.path.isabs(video_path):
            # Resolve relative to repo root (SCRIPT_DIR), not OUTPUT_DIR,
            # so "--video output/final.mp4" → output/final.mp4 (not output/output/final.mp4).
            video_path = os.path.normpath(os.path.join(SCRIPT_DIR, video_path))

        if not os.path.exists(video_path):
            err = f"Video not found: {video_path}"
            print(f"[ERROR] {err}")
            record_failure(os.path.basename(args.video), err)
            sys.exit(1)

        meta = load_meta()
        title = args.title or meta.get("title", os.path.splitext(os.path.basename(video_path))[0])
        description = args.description or meta.get("description", "")
        tags = args.tags or meta.get("tags", [])
        status = args.status

    if not tags:
        tags = ["ytshorts", "tech", "tutorial"]

    print(f"Video : {video_path}")
    print(f"Title : {title}")
    print(f"Desc  : {description[:80]}...")
    print(f"Tags  : {tags}")
    print(f"Status: {status}")

    # Check video file exists and has content
    if os.path.getsize(video_path) < 1000:
        err = f"Video file too small ({os.path.getsize(video_path)} bytes)"
        print(f"[ERROR] {err}")
        record_failure(os.path.basename(video_path), err)
        sys.exit(1)

    # Check ffmpeg for verification
    try:
        subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=format_name", video_path],
                       capture_output=True, check=True)
    except subprocess.CalledProcessError:
        err = f"ffprobe failed: {video_path} is not a valid video"
        print(f"[ERROR] {err}")
        record_failure(os.path.basename(video_path), err)
        sys.exit(1)

    if args.dry_run:
        print("[DRY RUN] Skipped upload.")
        return

    uploaded_url = run_upload(video_path, title, description, tags, status)

    # Batch mode: archive successfully uploaded files so the next cron tick
    # does not re-upload the same video.
    if args.batch and uploaded_url:
        day = datetime.now().strftime("%Y%m%d")
        archive_dir = os.path.join(OUTPUT_DIR, "archive", day)
        os.makedirs(archive_dir, exist_ok=True)
        for name in ("final.mp4", "meta.json"):
            src = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(archive_dir, name))
        pending = os.path.join(OUTPUT_DIR, "upload_pending.json")
        if os.path.exists(pending):
            os.remove(pending)
        with open(os.path.join(archive_dir, "uploaded_url.txt"), "w") as f:
            f.write(uploaded_url + "\n")
        print(f"[BATCH] Archived to {archive_dir}")



if __name__ == '__main__':
    main()
