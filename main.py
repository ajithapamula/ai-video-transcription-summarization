import os
import uuid
import shutil
import subprocess
import json
import logging
import re
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from typing import List
from urllib.parse import quote_plus
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pymongo import MongoClient
from pydub import AudioSegment
from deep_translator import GoogleTranslator
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
import openai
import time
import asyncio
import torch
import boto3
from botocore.exceptions import NoCredentialsError
from openai import OpenAI

client = OpenAI()

# === GPU CHECK ===
print("Using GPU:", torch.cuda.is_available())

# === INIT ===
app = FastAPI()
openai.api_key = os.getenv("OPENAI_API_KEY")

# === LOGGING ===
logger = logging.getLogger("video_processor")
logging.basicConfig(level=logging.INFO)

# === AWS CONFIG ===
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_S3_BUCKET = "connectly-storage"

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)

# === MONGO DB ===
mongo_user = quote_plus("connectly")
mongo_password = quote_plus("LT@connect25")
mongo_host = "192.168.48.201"
mongo_port = "27017"
MONGO_URI = f"mongodb://{mongo_user}:{mongo_password}@{mongo_host}:{mongo_port}/admin?authSource=admin"

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["sample_db"]
collection = db["test"]

# === HELPERS ===
def upload_to_s3(folder: str, file_path: str, file_name: str):
    try:
        s3_key = f"{folder}/{file_name}"
        # ✅ Remove ACL (new buckets do not support it)
        s3_client.upload_file(file_path, AWS_S3_BUCKET, s3_key)
        # Return public URL (works if your bucket policy allows read access)
        return f"https://{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        raise

def format_srt_time(seconds: float) -> str:
    td = timedelta(seconds=seconds)
    total = int(td.total_seconds())
    millis = int((td.total_seconds() - total) * 1000)
    return f"{str(timedelta(seconds=total)).zfill(8)},{millis:03}"

def create_srt_from_segments(segments: List[dict], output_path: str):
    if not segments:
        logger.warning(f"[WARN] No segments to write for {output_path}")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:05,000\n[No speech detected]\n\n")
        return

    logger.info(f"[INFO] Writing subtitles to {output_path} ({len(segments)} segments)")
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start = format_srt_time(seg.get("start", 0))
            end = format_srt_time(seg.get("end", 0))
            text = seg.get("text", "").strip()
            if not text:
                text = "[Silence]"
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

def generate_graph(dot_code: str, output_path: str):
    from graphviz import Source
    s = Source(dot_code)
    return s.render(filename=output_path, format="png", cleanup=True)

def save_docx(content: str, path: str, image_path: str = None, title: str = ""):
    doc = Document()
    if title:
        heading = doc.add_heading(title, level=1)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for line in content.splitlines():
        p = doc.add_paragraph(line.strip())
        p.style.font.size = Pt(12)

    if image_path and os.path.exists(image_path):
        doc.add_page_break()
        doc.add_heading("Mind Map", level=2)
        doc.add_picture(image_path, width=Inches(6))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.save(path)

async def transcribe_chunk(chunk_file: str, offset: float):
    """
    Transcribes a small WAV chunk using the GPT-4o transcribe API.
    Returns list of segment dicts with 'start', 'end', and 'text'.
    Compatible with latest OpenAI API (no verbose_json).
    """
    try:
        with open(chunk_file, "rb") as f:
            result = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=f,
                response_format="json"  # ✅ fixed: only 'json' or 'text' allowed
            )

        # ✅ Safely parse response
        text = result.text.strip() if hasattr(result, "text") else ""

        # Since no detailed timestamps are returned, create pseudo-segments
        # to preserve timing continuity for SRT generation
        approx_duration = 5  # seconds per chunk fallback
        segments = [{
            "start": offset,
            "end": offset + approx_duration,
            "text": text
        }]

        return segments

    except Exception as e:
        logger.error(f"[ERROR] Transcribing chunk failed: {chunk_file} - {e}")
        return []


def summarize_segment(transcript: str, context: str = ""):
    prompt = f"""
SYSTEM ROLE:
You are a SENIOR DOCUMENTATION AND TECHNICAL WRITING EXPERT.
You convert raw transcripts into COMPREHENSIVE, HIGHLY ACCURATE, FORMAL IMPLEMENTATION or STUDY GUIDES.

------------------------------------------------------------
WORD CORRECTION RULE:
If any word in the transcript appears to be a minor phonetic or spelling mismatch
(e.g., "ongo" → "MongoDB", "pie torch" → "PyTorch", "sequel" → "SQL"),
correct ONLY that mismatched term to its proper technical equivalent.
Do not rephrase sentences or modify correct words.
Maintain full sentence structure, meaning, and flow.
------------------------------------------------------------
You are a senior documentation and technical writing expert. Your task is to convert the following raw transcript segment into a comprehensive, highly accurate, and formal implementation or study guide based on the subject matter discussed.

The final output must:
- Be structured and formatted according to professional standards for enterprise-level training, onboarding, and technical enablement.
- Include step-by-step procedures, clearly numbered and logically ordered.
- Provide real-world tools, technologies, configurations, commands, and screenshots/images (placeholders if needed) relevant to the topic.
- Embed technical examples, use cases, CLI/GUI instructions, and expected outputs or screenshots where applicable.
- Cover common pitfalls, troubleshooting tips, and best practices to ensure full practical understanding.
- Use terminology and instructional depth suitable for readers to gain full conceptual and hands-on knowledge of the subject.
- The final document should resemble internal documentation used at organizations like SAP, Oracle, Java, Selenium, AI/ML, Data Science, AWS, Microsoft, or Google — clear, comprehensive, and instructional in tone.

Additionally, ensure that for every main topic, you provide 5–10 sentence descriptions that explain key concepts and their real-world applications.
------------------------------------------------------------
OBJECTIVE:
Create a detailed, real-world step-by-step implementation or process guide for [INSERT TOPIC/SUBJECT], designed specifically to support the creation of over 100 technical or comprehension questions.
------------------------------------------------------------
DOCUMENT FORMAT AND STRUCTURE RULES:
1. STRUCTURE — Use numbered sections/sub-sections (1, 1.1, etc.)
2. EACH SECTION MUST INCLUDE titles, procedures, validation, troubleshooting, and best practices.
3. VALIDATION — Describe how to confirm success.
4. TROUBLESHOOTING — Common issues, misconfigurations, fixes.
5. BEST PRACTICES — Efficiency, security, compliance.
6. CONCLUSION — Summarize what was done.
------------------------------------------------------------
IMPORTANT:
Replace sensitive data with placeholders (<ip>, <password>, etc.)
------------------------------------------------------------
COMBINED INPUT:
\"\"\"{transcript}\n\n{context}\"\"\" 
------------------------------------------------------------
FINAL INSTRUCTION:
Return only the fully formatted implementation or process guide including:
- TITLE, PURPOSE, PREREQUISITES
- STEP-BY-STEP instructions
- TROUBLESHOOTING, BEST PRACTICES, CONCLUSION
- A MIND MAP in DOT format at the end enclosed in triple backticks
End document with:
SUGGESTED NEXT STEPS: No specific next steps mentioned in this segment.
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a technical documentation assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
            max_tokens=2500  # new param in v1.x
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[ERROR] Summary generation failed: {e}")
        return "Summary generation failed."
def analyze_trainer_performance(transcript: str) -> dict:
    """
    Analyze trainer's technical content, explanation clarity, friendliness, and communication
    based purely on transcript semantics and delivery style markers.
    Uses GPT-4o for text-based behavioral and technical scoring.
    """
    if not transcript.strip():
        return {
            "technical_content": 0,
            "explanation_clarity": 0,
            "friendliness": 0,
            "communication": 0,
            "overall_feedback": "No speech detected for evaluation."
        } 

    prompt = f"""
You are an expert communication and training evaluator.
Evaluate the trainer's communication quality, tone, and content in the transcript below.

TRANSCRIPT:
\"\"\"{transcript}\"\"\"

Evaluate across the following dimensions (each scored 0–100%):
1. Technical Content — accuracy, depth, and domain clarity.
2. Explanation Clarity — how logically and simply ideas are explained.
3. Friendliness — warmth, politeness, and positive tone.
4. Communication – evaluate using Indian English standards:
   - Focus on fluency, confidence, and comfort with Indian accent.
   - Accept light Indianisms such as “basically”, “ok na”, “ya”, etc.
   - Penalize only unclear speech or excessive filler use (“umm”, “like”, “you know”).
   - Do not reduce marks for accent style — evaluate clarity, not foreign pronunciation.
Output a short, factual JSON report with numeric scores and 1–2 lines of feedback.
Format exactly as:

{{
  "technical_content": <number>,
  "explanation_clarity": <number>,
  "friendliness": <number>,
  "communication": <number>,
  "overall_feedback": "<short summary>"
}}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a behavioral analytics and technical communication evaluator."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )

        raw = response.choices[0].message.content.strip()
        # Extract JSON safely
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        else:
            logger.warning("⚠️ Unable to parse trainer evaluation JSON.")
            return {"error": "Failed to parse evaluation output."}

    except Exception as e:
        logger.error(f"[ERROR] Trainer performance evaluation failed: {e}")
        return {"error": str(e)}

# === VIDEO PROCESS ===
async def process_video(video_path: str, meeting_id: str, user_id: str):
    with TemporaryDirectory() as workdir:
        compressed = os.path.join(workdir, "compressed.mp4")
        audio_path = os.path.join(workdir, "audio.wav")

        # 1️⃣ Compression + Noise Cancellation
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-af", "afftdn=nf=-25",
            "-c:v", "libx264", "-crf", "35", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "64k", compressed
        ], check=True)

        # 2️⃣ Extract Audio for Transcription
        subprocess.run([
            "ffmpeg", "-y", "-i", compressed,
            "-ar", "16000", "-ac", "1", "-vn", audio_path
        ], check=True)

        # 3️⃣ Transcription (Whisper)
        audio = AudioSegment.from_wav(audio_path)
        chunk_length = 5 * 60 * 1000  # 5 mins
        audio_chunks = []
        for i in range(0, len(audio), chunk_length):
            chunk_file = os.path.join(workdir, f"chunk_{i//chunk_length}.wav")
            chunk = audio[i:i + chunk_length]
            chunk.export(chunk_file, format="wav")
            offset = i / 1000
            audio_chunks.append((chunk_file, offset))

        tasks = [transcribe_chunk(path, offset) for path, offset in audio_chunks]
        chunk_results = await asyncio.gather(*tasks)
        all_segments = [seg for result in chunk_results for seg in result]
        full_transcript = ''.join(seg['text'] for seg in all_segments)
        # 3️⃣.1 Trainer Performance Evaluation
        trainer_scores = analyze_trainer_performance(full_transcript)

        # 4️⃣ Subtitles Generation (English, Hindi, Telugu)
        srt_paths = {}
        for lang in ["en", "hi", "te"]:
            translated = []
            for seg in all_segments:
                text = seg.get("text", "")
                if lang != "en":
                    try:
                        text = GoogleTranslator(source='en', target=lang).translate(text)
                    except Exception:
                        text = "[Translation failed]"
                translated.append({
                    "start": seg.get("start", 0),
                    "end": seg.get("end", 0),
                    "text": text
                })
            srt_path = os.path.join(workdir, f"subs_{lang}.srt")
            create_srt_from_segments(translated, srt_path)
            srt_paths[lang] = srt_path

        # Debug: verify subtitle file
        print("🟦 Subtitle check:")
        print(f"  Exists: {os.path.exists(srt_paths['en'])}")
        if os.path.exists(srt_paths['en']):
            print(f"  Size: {os.path.getsize(srt_paths['en'])} bytes")

        # 5️⃣ Captioned Video Overlay (with correct FFmpeg escaping)
        captioned = os.path.join(workdir, "captioned.mp4")
        srt_path_fixed = os.path.abspath(srt_paths["en"]).replace("\\", "/")
        if not os.path.exists(srt_path_fixed):
            raise FileNotFoundError(f"Subtitle file missing: {srt_path_fixed}")

        cmd = [
            "ffmpeg", "-y",
            "-i", compressed,
            "-vf", f"subtitles={srt_path_fixed}:force_style='FontName=Arial\\,FontSize=18'",
            "-c:v", "libx264", "-c:a", "aac",
            captioned
        ]
        print("🟩 FFmpeg command:", " ".join(cmd))
        subprocess.run(cmd, check=True)

        # 6️⃣ Summary Generation (LLM)
        summary_text = summarize_segment(full_transcript)

        # 7️⃣ Mind Map Extraction & Image Creation
        image_path = os.path.join(workdir, "mindmap.png")
        image_url = None
        dot_match = re.search(r"```dot\s*(.*?)```", summary_text, re.DOTALL)
        if dot_match:
            dot_code = dot_match.group(1).strip()
            generate_graph(dot_code, image_path[:-4])
            image_url = upload_to_s3(
                "summary-image", image_path, f"{meeting_id}_{user_id}_mindmap.png"
            )

        # 8️⃣ DOCX Creation (Transcript + Summary)
        transcript_doc = os.path.join(workdir, "transcript.docx")
        summary_doc = os.path.join(workdir, "summary.docx")
        save_docx(full_transcript, transcript_doc, title="Full Transcript")
        save_docx(summary_text, summary_doc,
                  image_path if image_url else None, title="Summary Report")

        # 9️⃣ Uploads to AWS S3
        video_url = upload_to_s3("videos", captioned,
                                 f"{meeting_id}_{user_id}_captioned.mp4")
        transcript_url = upload_to_s3("transcripts", transcript_doc,
                                      f"{meeting_id}_{user_id}_transcript.docx")
        summary_url = upload_to_s3("summary", summary_doc,
                                   f"{meeting_id}_{user_id}_summary.docx")

        subtitle_urls = {}
        for lang, path in srt_paths.items():
            subtitle_urls[lang] = upload_to_s3(
                "videos", path, f"{meeting_id}_{user_id}_subs_{lang}.srt"
            )

        # 🔟 Save Metadata to MongoDB
        collection.insert_one({
            "meeting_id": meeting_id,
            "user_id": user_id,
            "filename": os.path.basename(video_path),
            "video_url": video_url,
            "transcript_url": transcript_url,
            "summary_url": summary_url,
            "image_url": image_url,
            "subtitles": subtitle_urls,
            "transcript_text": full_transcript,
            "summary_text": summary_text,
            "trainer_evaluation": trainer_scores,
            "timestamp": datetime.now()
        })

        # ✅ Return API Response
        return {
            "status": "success",
            "video_url": video_url,
            "transcript_url": transcript_url,
            "summary_url": summary_url,
            "summary_image_url": image_url,
            "subtitle_urls": subtitle_urls
        }

# === ROUTES ===
@app.post("/upload/")
async def upload_single(file: UploadFile = File(...), meeting_id: str = Form(...), user_id: str = Form(...)):
    try:
        existing = collection.find_one({"meeting_id": meeting_id, "user_id": user_id, "filename": file.filename})
        if existing:
            return {
                "status": "already_processed",
                "file": file.filename,
                "video_url": existing.get("video_url"),
                "transcript_url": existing.get("transcript_url"),
                "summary_url": existing.get("summary_url"),
                "summary_image_url": existing.get("image_url"),
                "subtitle_urls": existing.get("subtitles"),
                "message": "Already processed."
            }

        with TemporaryDirectory() as tmp:
            temp_path = os.path.join(tmp, file.filename)
            with open(temp_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            result = await process_video(temp_path, meeting_id, user_id)
            result["file"] = file.filename
            return JSONResponse(content=result)

    except Exception as e:
        logger.exception("Upload failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
@app.get("/health")
def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8010, reload=True)
