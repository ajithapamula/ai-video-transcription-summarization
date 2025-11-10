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
You are a senior documentation and technical writing expert. Your task is to convert the following raw transcript segment into a comprehensive, highly accurate, and formal implementation or study guide based on the subject matter discussed.

The final output must:

- Be structured and formatted according to professional standards for enterprise-level training, onboarding, line pictures, and technical enablement.
- Include step-by-step procedures, clearly numbered and logically ordered.
- Provide real-world tools, technologies, configurations, commands, and screenshots/images (placeholders if needed) relevant to the topic.
- Embed technical examples, use cases, CLI/GUI instructions, and expected outputs or screenshots where applicable.
- Cover common pitfalls, troubleshooting tips, and best practices to ensure full practical understanding.
- Use terminology and instructional depth suitable for readers to gain 100% conceptual and hands-on knowledge of the subject.
- The final document should resemble internal documentation used at organizations like SAP, Oracle, Java, Selenium, AI/ML, Data Science, AWS, Microsoft, or Google — clear, comprehensive, and instructional in tone.

- Additionally, ensure that **for every main topic, you provide 5-10 sentence descriptions** that explain key concepts and their real-world applications. For example, for "Oracle Database" or "Generative AI," give a clear explanation, its use cases, and why it is essential for enterprises. Avoid high-level jargon. Make it practical, applicable, and understandable.

---

OBJECTIVE:

Create a detailed, real-world step-by-step implementation or process guide for [INSERT TOPIC/SUBJECT], designed specifically to support the creation of over 100 technical or comprehension questions. The guide must:

- Reflect real-world tools, technologies, workflows, and industry terminology.
- Break down each phase of the implementation or process logically and sequentially.
- Include practical examples, code snippets (if applicable), key decisions, best practices, and commonly used tools at each step.
- Highlight common challenges or misconceptions, and how they’re addressed in real practice.
- Use terminology and structure that would support SMEs or instructional designers in generating high-quality technical questions based on the guide.
- Avoid abstract or overly generic statements — focus on precision, clarity, and applied knowledge.

---

DOCUMENT FORMAT & STRUCTURE RULES:

1. STRUCTURE
- Use numbered sections and sub-sections (e.g., 1, 1.1, 1.2.1)
- No markdown, emojis, or decorative formatting
- Use plain, formal, enterprise-grade language

2. EACH SECTION MUST INCLUDE:
- A *clear title* and *brief purpose statement*
- *Step-by-step technical or procedural instructions*, including:
    - All relevant tools, platforms, or interfaces used (if any)
    - Any paths, commands, actions, configurations, or API calls involved
    - All required inputs, values, parameters, or dependencies
    - A logical sequence of operations, clearly numbered or separated by actionable steps
    - Tips, warnings, and Important Notes, or expected outcomes where necessary
- **5-10 sentence description** of each main topic, explaining what the concept is, its use cases, and real-world applications. This should be clear and concise for technical audiences to understand why the topic is essential and how it fits into practical workflows.

3. VALIDATION

- Describe how to confirm success (e.g., Expected Outputs, System or Health Checks, Technical and Functional Verifications, Visual Indicators, Fallback/Error Conditions indicators)

4. TROUBLESHOOTING (if applicable)

- Clearly list frequent or known issues that may arise during or after the procedure
- Describe the conditions or misconfigurations that typically lead to each issue
- Provide step-by-step corrective actions or configuration changes needed to resolve each problem
- Mention specific file paths, log viewer tools, console commands, or dashboard areas where errors and diagnostics can be found
- Include example error codes or system messages that help in identifying the issue

5. BEST PRACTICES

- You are a senior technical writer. Based on the following transcript or topic, create a BEST PRACTICES section suitable for formal technical documentation, onboarding materials, or enterprise IT guides.
- Efficiency improvements (e.g., time-saving configurations, automation tips)
- Security or compliance tips (e.g., encryption, IAM roles, audit logging)
- Standard operating procedures (SOPs) used in enterprise environments
- Avoided pitfalls and why they should be avoided
- Format the content using bullet points or short sections for clarity and actionability.
- Avoid vague, obvious, or overly general suggestions — focus on real-world, practical insights derived from field experience or best-in-class implementation norms.

6. CONCLUSION
- Summarize what was implemented or discussed
- Confirm expected outcomes and readiness indicators

---

IMPORTANT:
If the input contains any values such as usernames, IP addresses, server names, passwords, port numbers, or similar technical identifiers — replace their actual content with generic XML-style tags, while preserving the sentence structure and purpose. For example:

- Replace any specific IP address with: <ip>
- Replace any actual password or secret with: <password>
- Replace any actual hostname with: <hostname>
- Replace any actual port number with: <port>
- Replace any username with: <username>
- Replace any email with: <email>

Do NOT alter the sentence structure, meaning, or flow — keep the language intact while swapping the actual values with tags
Do not display or retain real values — just show the placeholder tag. Maintain the original meaning and flow of the instructions.
Format the output as clean, professional documentation, suitable for inclusion in implementation guides, SOPs, or training materials.
Highlight any placeholders in a way that makes it easy for the user to identify where to substitute their own values later.

---

Also:
- Cross-check all tools, commands, file paths, service names, APIs, and utilities with reliable, real-world sources (e.g., official vendor documentation, widely accepted best practices).

 1. If something appears ambiguous, incorrect, or outdated, correct it to its current, supported version.
 2. Use only commands, APIs, or tool names that are verifiably valid and relevant to the topic context.
- Consolidate duplicate or fragmented instructions:
 1. If a step or process is repeated across segments, merge them into a single, complete, and accurate version.
 2. Remove redundancy and preserve the most detailed and correct version of each step.
 3. Do NOT include deprecated or unverifiable content:
 4. Exclude outdated commands, legacy references, or tools no longer maintained.
 5. Replace such content with modern equivalents where available.

- Output the final result as a formal technical guide, with:
  1. Clear section headings
  2. Correct and tested commands/scripts
  3. Accurate tool names and workflows
  4. Logical flow suitable for developers, engineers, or IT teams

---

COMBINED INPUT:
\"\"\"{transcript}\n\n{context}\"\"\"

---

FINAL INSTRUCTION:
Return only the fully formatted implementation or process guide includes below

- A clear, descriptive title
- A concise purpose statement or overview
- Prerequisites and tools required
- Numbered step-by-step instructions with:
   1. Commands, paths, configuration settings, or code blocks (as needed)
   2. GUI or CLI actions explained clearly
   3. Expected inputs, parameters, or options
   4. Confirmation of success (outputs, logs, tests, or validation steps)
   5. Troubleshooting (common issues, causes, and resolutions — if applicable)
   6. Best Practices (efficiency, reliability, security — if applicable)
   7. **Include a mind map diagram in DOT format enclosed in triple backticks at the end**
   8. **Insert chart/diagram placeholders inline to represent where the visual mind map image should appear**

- Replace any real usernames, IP addresses, passwords, ports, or hostnames with <username>, <ip>, <password>, <port>, or <hostname> where needed.
- Eliminate all redundant or outdated, abused content. Only use valid and current tools and commands.

End Document with Standardized "Suggested Next Steps" Note  
*Suggested next steps: No specific next steps mentioned in this segment.*
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
