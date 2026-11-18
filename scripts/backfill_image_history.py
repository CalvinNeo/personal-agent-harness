"""Recover proven image inputs from existing conversations and Codex records.

Run while AstrBot is stopped. Reads source state without changing it. Output
contains counts only; no chat text, participant IDs, paths or image URLs.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from image_context.archive import ImageArchive, normalize_for_model, read_temp_image


def scope(umo):
    return umo, "__group__" if ":GroupMessage:" in umo else umo.rsplit(":", 1)[-1]


def recover(data_root: Path, runner_root: Path | None, archive_root: Path, journal_file: Path | None = None):
    config = json.loads((data_root / "config/image_context_config.json").read_text(encoding="utf-8-sig"))
    allowed = set(config.get("allowed_umos") or [])
    archive = ImageArchive(archive_root)
    result = {"conversation_images": 0, "codex_images": 0, "received_uploads": 0, "skipped": 0, "scopes": 0}
    seen_scopes = set()
    connection = sqlite3.connect("file:" + str(data_root / "data_v4.db") + "?mode=ro", uri=True)
    for cid, umo, created, raw in connection.execute(
            "SELECT conversation_id,user_id,created_at,content FROM conversations"):
        if umo not in allowed:
            continue
        for number, message in enumerate(json.loads(raw or "[]")):
            if message.get("role") != "user" or not isinstance(message.get("content"), list):
                continue
            parts = message["content"]
            text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
            stamp = re.search(r"Current datetime: (\d{4}-\d\d-\d\d \d\d:\d\d)", text)
            when = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() if stamp else datetime.fromisoformat(str(created)).replace(tzinfo=ZoneInfo("UTC")).timestamp()
            images = []
            original_paths = re.findall(r"original path ([^\]\n]+)", text)
            for part in parts:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                ref = part.get("image_url", {}).get("url", "")
                try:
                    if len(original_paths) > len(images):
                        try:
                            raw_image = read_temp_image(original_paths[len(images)], data_root / "temp")
                        except Exception:
                            raw_image = base64.b64decode(ref.split(",", 1)[1], validate=True) if ref.startswith("data:image/") else read_temp_image(ref, data_root / "temp")
                    elif ref.startswith("data:image/") and ";base64," in ref:
                        raw_image = base64.b64decode(ref.split(",", 1)[1], validate=True)
                    else:
                        raw_image = read_temp_image(ref, data_root / "temp")
                    normalize_for_model(raw_image)
                    images.append((raw_image, "legacy-conversation"))
                except Exception:
                    result["skipped"] += 1
            if images:
                archive.ingest(*scope(umo), f"history-{cid}-{number}", images, received_wall_at=when,
                               source="reply" if "<Quoted Message>" in text else "upload")
                result["conversation_images"] += len(images)
                seen_scopes.add(umo)
    connection.close()
    if journal_file:
        pending = None
        image_files = list((data_root / "temp").glob("media_image_*"))
        for line in journal_file.read_text().splitlines():
            line = re.sub(r"\x1b\[[0-9;]*[mK]", "", line)
            timestamp = re.match(r"(\S+)", line)
            if not timestamp:
                continue
            try:
                received = datetime.fromisoformat(timestamp.group(1)).timestamp()
            except ValueError:
                continue
            if "[core.event_bus:" in line:
                sender = re.search(r"/([A-Za-z0-9_-]+):\s+(.*)$", line)
                pending = (received, sender.group(1)) if sender and "[图片]" in sender.group(2) else None
            cached = re.search(r"Image cache received=(\d+) accepted=(\d+) scope=([a-f0-9]{12})", line)
            if not cached or pending is None or not 0 <= received - pending[0] <= 10:
                continue
            matched = [umo for umo in allowed if hashlib.sha256((umo + "\0" + pending[1]).encode()).hexdigest()[:12] == cached.group(3)]
            if len(matched) != 1 or int(cached.group(2)) <= 0:
                continue
            umo = matched[0]
            images = []
            for file in image_files:
                stamp = re.match(r"media_image_(\d{17})_", file.name)
                if not stamp:
                    continue
                file_at = datetime.strptime(stamp.group(1), "%Y%m%d%H%M%S%f").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
                if not pending[0] <= file_at <= received + 1:
                    continue
                try:
                    data = read_temp_image(str(file), data_root / "temp")
                    normalize_for_model(data)
                    images.append((data, "verified-received-upload"))
                except Exception:
                    result["skipped"] += 1
            if images:
                event_id = "received-" + hashlib.sha256((str(pending[0]) + cached.group(3)).encode()).hexdigest()
                archive.ingest(*scope(umo), event_id, images, received_wall_at=pending[0], source="upload")
                result["received_uploads"] += len(images)
                seen_scopes.add(umo)
            pending = None
    routes_path = data_root / "plugins/codex_bridge/jobs.json"
    if runner_root and routes_path.is_file():
        routes = json.loads(routes_path.read_text()).get("jobs", [])
        for route in routes:
            umo, sender, cid = route.get("umo"), route.get("sender"), route.get("conversation_id") or route.get("id")
            if umo not in allowed or not re.fullmatch(r"[0-9a-f]{16}", str(cid)) or not isinstance(sender, str):
                continue
            owner = hashlib.sha256((umo + "\0" + sender).encode()).hexdigest()
            record = runner_root / "jobs/conversations" / (cid + ".json")
            if not record.is_file() or record.is_symlink():
                continue
            conversation = json.loads(record.read_text())
            if conversation.get("owner") != owner:
                continue
            digest = hashlib.sha256((owner + "\0" + cid).encode()).hexdigest()[:32]
            directory = runner_root / "workspaces" / ("conversation-" + digest) / "workspace/input"
            images = []
            for item in conversation.get("images", []):
                name, expected = item.get("name"), item.get("digest")
                if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|jpeg|png)", name):
                    result["skipped"] += 1
                    continue
                try:
                    raw_image = read_temp_image(str(directory / name), directory)
                    if hashlib.sha256(raw_image).hexdigest() != expected:
                        raise ValueError("digest mismatch")
                    normalize_for_model(raw_image)
                    images.append((raw_image, "legacy-codex"))
                except Exception:
                    result["skipped"] += 1
            if images:
                archive.ingest(*scope(umo), "codex-" + cid, images,
                               received_wall_at=conversation["created"], source="upload")
                result["codex_images"] += len(images)
                seen_scopes.add(umo)
    result["scopes"] = len(seen_scopes)
    result["ok"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-data", type=Path, required=True)
    parser.add_argument("--runner-data", type=Path)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--journal-file", type=Path)
    args = parser.parse_args()
    print(json.dumps(recover(args.astrbot_data, args.runner_data, args.archive_dir, args.journal_file), ensure_ascii=False))
