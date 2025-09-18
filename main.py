#!/usr/bin/env python3
"""
Telegram → KIE (Runway via KIE) → concat → Telegram
- Supports two modes:
  1) POLLING (default): no public URL needed. Set USE_KIE_POLLING=1 in .env
  2) CALLBACK: uses /kie/callback and a public CALLBACK_BASE_URL

Env (.env next to this script):
TELEGRAM_TOKEN=...
KIE_API_KEY=...
KIE_BASE_URL=https://api.kie.ai
RUNWAY_MODEL=runway-duration-5-generate
DEFAULT_AR=9:16
DEFAULT_SCENE_SECONDS=4
DEFAULT_SCENE_COUNT=1           # start with 1 for fast tests
DEFAULT_FPS=24
USE_KIE_POLLING=1               # <-- default to polling
# Optional concat via FAL:
FAL_API_KEY=
FAL_CONCAT_URL=
# Optional dev:
ALLOW_TG_FILE_URL=0
PORT=8080
CALLBACK_BASE_URL=              # only needed for callback mode
"""

import os, io, re, asyncio, logging, base64, uuid
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import aiohttp
from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------- logging ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)

# ---------- env ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
KIE_API_KEY = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai")
RUNWAY_MODEL = os.getenv("RUNWAY_MODEL", "runway-duration-5-generate")
DEFAULT_AR = os.getenv("DEFAULT_AR", "9:16")
DEFAULT_SCENE_SECONDS = int(os.getenv("DEFAULT_SCENE_SECONDS", "4"))
DEFAULT_SCENE_COUNT = int(os.getenv("DEFAULT_SCENE_COUNT", "1"))
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "24"))
USE_KIE_POLLING = os.getenv("USE_KIE_POLLING", "1") == "1"   # 👈 default ON
ALLOW_TG_FILE_URL = os.getenv("ALLOW_TG_FILE_URL", "0") == "1"
CALLBACK_BASE_URL = os.getenv("CALLBACK_BASE_URL", "")
PORT = int(os.getenv("PORT", "8080"))

FAL_API_KEY = os.getenv("FAL_API_KEY", "")
FAL_CONCAT_URL = os.getenv("FAL_CONCAT_URL", "https://api.fal.ai/v1/functions/YOUR_FAL_CONCAT_ID/invoke")

RUNWAY_QUALITY = os.getenv("RUNWAY_QUALITY", "720p")

ALLOWED_DURATIONS = (5, 8, 10)
def clamp_duration(s: int) -> int:
    try: s = int(s)
    except Exception: s = 5
    return min(ALLOWED_DURATIONS, key=lambda d: abs(d - s))

def pick_quality(duration: int, prefer: str = RUNWAY_QUALITY) -> str:
    q = (prefer or "720p").lower()
    if q not in ("720p", "1080p"):
        q = "720p"
    # 1080p is only allowed when duration==5
    if q == "1080p" and clamp_duration(duration) != 5:
        logging.info("Downgrading quality 1080p→720p for duration=%s", duration)
        q = "720p"
    return q



ALLOWED_DURATIONS = (5, 8, 10)
def clamp_duration(s: int) -> int:
    try:
        s = int(s)
    except Exception:
        s = 5
    return min(ALLOWED_DURATIONS, key=lambda d: abs(d - s))


# ---------- state ----------
BATCHES: Dict[str, Dict] = {}

# ---------- utils ----------
def shotlist_from_prompt(prompt: str, count: int = DEFAULT_SCENE_COUNT, seconds: int = DEFAULT_SCENE_SECONDS) -> List[Dict]:
    prompt = (prompt or "cinematic character video").strip()
    templates = [
        "establishing portrait, soft key light, shallow depth of field",
        "medium shot, gentle camera push-in, natural motion",
        "close-up with subtle parallax and bokeh highlights",
        "action beat with dynamic angle change",
        "ambient cutaway, hands/prop detail, moody lighting",
    ]
    return [{"prompt": f"{prompt}, {templates[i % len(templates)]}", "seconds": seconds} for i in range(count)]


async def download_file_from_telegram(bot, file_id: str) -> Tuple[bytes, str]:
    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)

    # Build a fetchable URL for third parties
    path = getattr(tg_file, "file_path", "")
    token = TELEGRAM_TOKEN

    # If Telegram ever returns a full URL here, use it as-is.
    if isinstance(path, str) and re.match(r"^https?://", path):
        file_url = path
    else:
        file_url = f"https://api.telegram.org/file/bot{token}/{path.lstrip('/')}"
    logging.info("[TG file] path=%s -> url=%s", path, file_url)

    return buf.getvalue(), file_url


def pick_photo_file_id(message) -> Optional[str]:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id
    if message.reply_to_message and message.reply_to_message.photo:
        return message.reply_to_message.photo[-1].file_id
    return None


async def kie_generate(session: ClientSession, *, scene_prompt: str, image_url: Optional[str],
                       aspect_ratio: str, callback_url: str, duration: int) -> Dict:
    url = f"{KIE_BASE_URL.rstrip('/')}/api/v1/runway/generate"
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": scene_prompt,
        "duration": clamp_duration(duration),
        "quality": pick_quality(duration),
        "aspectRatio": aspect_ratio,
        "model": RUNWAY_MODEL,
        "callBackUrl": callback_url,
    }
    if image_url:
        payload["imageUrl"] = image_url
    async with session.post(url, json=payload, headers=headers, timeout=600) as resp:
        data = await resp.json()
    logging.info("[KIE generate (cb)] status=%s body=%s", resp.status, data)
    if (data.get("code") or 200) != 200 or resp.status >= 300:
        raise RuntimeError(f"KIE generate error code={data.get('code')}: {data.get('msg')}")
    return data


async def kie_generate_task(session, *, scene_prompt, image_url, aspect_ratio, duration):
    url = f"{KIE_BASE_URL.rstrip('/')}/api/v1/runway/generate"
    headers = {"Authorization": f"Bearer {KIE_API_KEY}", "Content-Type":"application/json"}
    payload = {
        "prompt": scene_prompt,
        "duration": clamp_duration(duration),
        "quality": pick_quality(duration),
        "aspectRatio": aspect_ratio,
        "model": RUNWAY_MODEL,
    }
    if image_url:
        payload["imageUrl"] = image_url
    async with session.post(url, json=payload, headers=headers, timeout=600) as r:
        try:
            data = await r.json()
        except Exception:
            txt = await r.text()
            raise RuntimeError(f"KIE generate non-JSON ({r.status}): {txt[:400]}")
    logging.info("[KIE generate] status=%s body=%s", r.status, data)
    if (data.get("code") or 200) != 200:
        raise RuntimeError(f"KIE generate error code={data.get('code')}: {data.get('msg')}")
    d = data.get("data") or {}
    task_id = d.get("taskId") or d.get("task_id") or data.get("task_id")
    if not task_id:
        raise RuntimeError(f"No task_id in KIE response: {data}")
    return task_id


async def kie_get_details(session, task_id):
    headers = {"Authorization": f"Bearer {KIE_API_KEY}"}
    url = f"{KIE_BASE_URL.rstrip('/')}/api/v1/runway/record-detail?taskId={task_id}"
    async with session.get(url, headers=headers, timeout=60) as r:
        data = await r.json()
    logging.info("[KIE details] %s -> %s", url, data)
    return data

async def kie_poll_video_url(session, task_id, poll_every=7, timeout_s=900):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        data = await kie_get_details(session, task_id)
        code = data.get("code")
        d = data.get("data") or {}
        state = (d.get("state") or "").lower()   # wait | queueing | generating | success | fail
        vi = d.get("videoInfo") or {}
        video_url = vi.get("videoUrl") or d.get("video_url")  # primary per docs

        if video_url:
            logging.info("[KIE poll] READY task=%s url=%s", task_id, video_url)
            return video_url
        if state == "fail" or (code and int(code) != 200):
            raise RuntimeError(f"KIE job failed: {data}")
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"KIE job {task_id} timed out; last={data}")
        logging.info("[KIE poll] task=%s state=%s (waiting)", task_id, state)
        await asyncio.sleep(poll_every)



# ---------- concat ----------
async def fal_concat(session: ClientSession, urls: List[str]) -> Optional[str]:
    if not FAL_API_KEY or not FAL_CONCAT_URL or "YOUR_FAL_CONCAT_ID" in FAL_CONCAT_URL:
        return None
    headers = {"Authorization": f"Bearer {FAL_API_KEY}", "Content-Type": "application/json"}
    transitions = [{"url": u, "transition": "crossfade" if i else "none", "duration": 0.5} for i, u in enumerate(urls)]
    payload = {"inputs": transitions, "output": {"format": "mp4", "fps": DEFAULT_FPS}}
    async with session.post(FAL_CONCAT_URL, json=payload, headers=headers, timeout=1800) as resp:
        data = await resp.json()
    if resp.status >= 300:
        return None
    url = data.get("result", {}).get("url") or data.get("output", {}).get("url")
    if url:
        return url
    status_url = data.get("status_url") or data.get("statusUrl") or data.get("urls", {}).get("status")
    if not status_url:
        return None
    while True:
        async with session.get(status_url, headers=headers, timeout=600) as r2:
            s = await r2.json()
        st = (s.get("status") or "").lower()
        if st in {"succeeded", "success", "completed", "ready"}:
            return s.get("result", {}).get("url") or s.get("output", {}).get("url")
        if st in {"failed", "error", "cancelled", "canceled"}:
            return None
        await asyncio.sleep(5)

async def local_concat(urls: List[str]) -> str:
    from moviepy.editor import VideoFileClip, concatenate_videoclips
    import urllib.request, shutil, tempfile, os as _os
    tmp_dir = tempfile.mkdtemp(prefix="concat_")
    local_paths = []
    for i, u in enumerate(urls):
        p = _os.path.join(tmp_dir, f"part_{i}.mp4")
        with urllib.request.urlopen(u) as r, open(p, "wb") as f:
            shutil.copyfileobj(r, f)
        local_paths.append(p)
    clips = [VideoFileClip(p) for p in local_paths]
    try:
        final = concatenate_videoclips(clips, method="compose")
        out_path = _os.path.join(tmp_dir, "final.mp4")
        final.write_videofile(out_path, fps=DEFAULT_FPS, codec="libx264", audio_codec="aac")
    finally:
        for c in clips:
            try: c.close()
            except: pass
    return out_path

# ---------- Telegram handlers ----------
HELP_TEXT = (
    "Send a photo with a caption like:\n"
    "  /video cyberpunk fox in neon alley\n\n"
    "Or reply /video <prompt> to a photo.\n"
    "I’ll generate a multi-scene clip and send it back."
)

def pick_prompt_and_file_id(msg) -> (str, Optional[str]):
    caption_or_text = (msg.caption or msg.text or "").strip()
    prompt = re.sub(r"^/(video|start)\s*", "", caption_or_text, flags=re.I).strip()
    return prompt, pick_photo_file_id(msg)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "Polling" if USE_KIE_POLLING else "Callback"
    await update.message.reply_text(HELP_TEXT + f"\n\nUsing provider: KIE • Mode: {mode}")

async def ping_cmd(update: Update, context):
    await update.message.reply_text("pong ✅")

async def debug_all(update: Update, context):
    logging.info("UPDATE RECEIVED: %s", update.to_dict())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    prompt, file_id = pick_prompt_and_file_id(msg)
    if not prompt and not file_id:
        await msg.reply_text("Please send a photo with a caption (your prompt), or reply /video <prompt> to a photo.")
        return

    status = await msg.reply_text("Got it — sending to KIE… 🎬")

    try:
        image_url = None
        if file_id:
            raw, tg_url = await download_file_from_telegram(context.bot, file_id)
            image_url = tg_url if ALLOW_TG_FILE_URL else None

        scenes = shotlist_from_prompt(prompt)

        async with aiohttp.ClientSession() as session:
            if USE_KIE_POLLING:
                # --- POLLING (no public URL needed) ---
                async def gen_scene(sc):
                    task_id = await kie_generate_task(
                        session,
                        scene_prompt=sc["prompt"],
                        image_url=image_url,
                        aspect_ratio=DEFAULT_AR,
                        duration=sc["seconds"],  # 👈 pass seconds for clamping
                    )
                    logging.info("[KIE] started task: %s", task_id)
                    return await kie_poll_video_url(session, task_id)

                urls = await asyncio.gather(*[gen_scene(s) for s in scenes], return_exceptions=True)
                scene_urls = [u for u in urls if isinstance(u, str)]
                # after: scene_urls = [u for u in urls if isinstance(u, str)]
                if not scene_urls:
                    raise RuntimeError("No scenes succeeded.")

                final_url = None
                local_path = None

                if len(scene_urls) == 1:
                    # ✅ single scene: send as-is, no concat needed
                    final_url = scene_urls[0]
                else:
                    # multi-scene: try FAL, else local concat
                    final_url = await fal_concat(session, scene_urls)
                    if not final_url:
                        local_path = await local_concat(scene_urls)

                if final_url:
                    await context.bot.send_video(chat_id=chat_id, video=final_url, caption=f"✨ {prompt[:120]}")
                else:
                    with open(local_path, "rb") as f:
                        await context.bot.send_video(chat_id=chat_id, video=f, caption=f"✨ {prompt[:120]}")
                await status.edit_text("Done ✅")

                errors = [e for e in urls if isinstance(e, Exception)]
                if not scene_urls:
                    raise RuntimeError(f"KIE scene error: {errors[0] if errors else 'Unknown error'}")

                final_url = await fal_concat(session, scene_urls)
                local_path = None
                if not final_url:
                    local_path = await local_concat(scene_urls)

                if final_url:
                    await context.bot.send_video(chat_id=chat_id, video=final_url, caption=f"✨ {prompt[:120]}")
                else:
                    with open(local_path, "rb") as f:
                        await context.bot.send_video(chat_id=chat_id, video=f, caption=f"✨ {prompt[:120]}")
                await status.edit_text("Done ✅")

            else:
                # --- CALLBACK (needs public CALLBACK_BASE_URL) ---
                if not CALLBACK_BASE_URL:
                    await status.edit_text("Set CALLBACK_BASE_URL to a public https URL so KIE can call back (ngrok/Cloud Run/etc.).")
                    return
                batch_id = str(uuid.uuid4())
                BATCHES[batch_id] = {"chat_id": chat_id, "total": len(scenes), "urls": {}, "prompt": prompt}
                for i, sc in enumerate(scenes):
                    cb = f"{CALLBACK_BASE_URL.rstrip('/')}/kie/callback?batch={batch_id}&scene={i}&total={len(scenes)}"
                    await kie_generate(session,
                                       scene_prompt=sc["prompt"],
                                       image_url=image_url,
                                       aspect_ratio=DEFAULT_AR,
                                       callback_url=cb,
                                       duration=sc["seconds"],  # 👈 pass seconds for clamping
                                       )
                await status.edit_text(f"Scenes dispatched ✅  Batch: {batch_id}\nI’ll send the final video when renders complete.")
    except Exception as e:
        logging.exception("handle_message error")
        await status.edit_text(f"Failed ❌ {e}")

# ---------- KIE callback route (only used in callback mode) ----------
async def kie_callback(request: web.Request):
    try:
        params = request.rel_url.query
        batch = params.get("batch")
        scene = int(params.get("scene", "0"))
        total = int(params.get("total", "1"))
        payload = await request.json()

        code = payload.get("code")
        d = payload.get("data") or {}
        video_url = d.get("video_url")
        task_id = d.get("task_id")
        logging.info("[KIE callback] code=%s task=%s url=%s", code, task_id, bool(video_url))
    except Exception as e:
        return web.Response(status=400, text=f"Bad request: {e}")

    if not batch:
        return web.Response(status=400, text="Missing batch id")

    if code == 200 and video_url:
        entry = BATCHES.setdefault(batch, {"chat_id": None, "total": total, "urls": {}, "prompt": ""})
        entry["urls"][scene] = video_url
        entry["total"] = total
    else:
        return web.json_response({"status": "received", "ok": False})

    entry = BATCHES.get(batch, {})
    chat_id = entry.get("chat_id")
    if chat_id is not None and len(entry.get("urls", {})) >= entry.get("total", 1):
        urls = [entry["urls"][i] for i in sorted(entry["urls"].keys())]
        async with aiohttp.ClientSession() as session:
            final_url = await fal_concat(session, urls)
        if final_url:
            await request.app["tg_app"].bot.send_video(chat_id=chat_id, video=final_url, caption=f"✨ {entry.get('prompt','')[:120]}")
        else:
            # fallback local concat
            from moviepy.editor import VideoFileClip, concatenate_videoclips
            import urllib.request, shutil, tempfile, os as _os
            tmp_dir = tempfile.mkdtemp(prefix="concat_")
            local_paths = []
            for i, u in enumerate(urls):
                p = _os.path.join(tmp_dir, f"part_{i}.mp4")
                with urllib.request.urlopen(u) as r, open(p, "wb") as f:
                    shutil.copyfileobj(r, f)
                local_paths.append(p)
            clips = [VideoFileClip(p) for p in local_paths]
            try:
                final = concatenate_videoclips(clips, method="compose")
                out_path = _os.path.join(tmp_dir, "final.mp4")
                final.write_videofile(out_path, fps=DEFAULT_FPS, codec="libx264", audio_codec="aac")
                await request.app["tg_app"].bot.send_video(chat_id=chat_id, video=open(out_path, "rb"), caption=f"✨ {entry.get('prompt','')[:120]}")
            finally:
                for c in clips:
                    try: c.close()
                    except: pass
        try: del BATCHES[batch]
        except KeyError: pass

    return web.json_response({"status": "received", "ok": True})
 
# ---------- bootstrap ----------
async def main_async():
    if not TELEGRAM_TOKEN or not KIE_API_KEY:
        raise SystemExit("Set TELEGRAM_TOKEN and KIE_API_KEY (.env supported).")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.ALL, debug_all), group=1)

    web_app = web.Application()
    web_app["tg_app"] = app
    web_app.router.add_post("/kie/callback", kie_callback)

    await app.initialize()
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    logging.info("Starting bot as @%s (id=%s) • Mode=%s", me.username, me.id, "Polling" if USE_KIE_POLLING else "Callback")

    await app.start()
    await app.updater.start_polling()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"Bot running. Local callback: http://0.0.0.0:{PORT}/kie/callback")
    if not USE_KIE_POLLING and CALLBACK_BASE_URL:
        print(f"(Public) {CALLBACK_BASE_URL.rstrip('/')}/kie/callback")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main_async())
