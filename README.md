🎥 AI Video Transcription & Summarization Service
🧠 Overview

This project provides a complete AI-driven pipeline that:

Accepts video uploads via a REST API.

Extracts and denoises audio.

Uses OpenAI Whisper for transcription.

Generates multilingual subtitles (English, Hindi, Telugu).

Creates an AI-generated technical summary using GPT-4.

Builds a mind map (DOT → Graphviz → PNG).

Produces formatted .docx reports.

Uploads results to AWS S3.

Stores metadata and links in MongoDB.

It’s designed for enterprise use cases such as:

Meeting and lecture documentation

Training video summarization

Knowledge base generation

Technical documentation automation

🏗️ Architecture

'''
 ┌────────────┐     ┌──────────────┐     ┌───────────┐
 │  FastAPI   │───▶ │  Processing  │───▶ │  MongoDB  │
 │  Endpoint  │     │  Pipeline    │     └───────────┘
 │ (Upload)   │     │ (FFmpeg +    │
 └─────┬──────┘     │ Whisper + AI │     ┌───────────┐
       │             │  Summarizer) │───▶ │   AWS S3  │
       │             └──────────────┘     └───────────┘
       │
       ▼
  🎬  User Uploads
'''

🚀 Features

🎧 Audio extraction and noise reduction using FFmpeg

🗣️ Speech-to-text via OpenAI Whisper

🌍 Multilingual subtitle generation (English, Hindi, Telugu)

🧾 AI-driven summary generation using GPT-4

🗺️ Mind map visualization from summary (via Graphviz)

🧰 Document export in .docx (Transcript + Summary)

☁️ Automatic upload to AWS S3

🗄️ Metadata persistence in MongoDB

🧩 Tech Stack
Component	Technology
Backend API	FastAPI
Transcription	OpenAI Whisper
AI Summarization	GPT-4 (via OpenAI API)
Audio Processing	FFmpeg, Pydub
Translation	Deep Translator
Visualization	Graphviz
Storage	AWS S3
Database	MongoDB
Document Export	Python-Docx

⚙️ Setup Instructions
1. Clone Repository
git clone https://github.com/<your-username>/ai-video-summary.git
cd ai-video-summary

2. Create Virtual Environment
python3 -m venv venv
source venv/bin/activate   # (Linux/Mac)
venv\Scripts\activate      # (Windows)

3. Install Dependencies
pip install -r requirements.txt


If you don’t have ffmpeg, install it:

sudo apt install ffmpeg

4. Set Environment Variables

Create a .env file or export variables:

export OPENAI_API_KEY="your_openai_api_key"
export AWS_ACCESS_KEY_ID="your_aws_access_key"
export AWS_SECRET_ACCESS_KEY="your_aws_secret_key"
export AWS_REGION="ap-south-1"


Update your MongoDB credentials and host inside the script or load them from .env.

▶️ Run the Server
uvicorn main:app --host 0.0.0.0 --port 8010 --reload


Then open your browser:

http://localhost:8010

📡 API Endpoints
POST /upload/

Description: Upload a video file for processing.

Form Data:

Field	Type	Description
file	File	Video file (mp4, mov, etc.)
meeting_id	string	Unique meeting identifier
user_id	string	Unique user identifier

Response Example:

{
  "status": "success",
  "video_url": "https://s3.amazonaws.com/connectly-storage/videos/meeting1_user1_captioned.mp4",
  "transcript_url": "https://s3.amazonaws.com/connectly-storage/transcripts/meeting1_user1_transcript.docx",
  "summary_url": "https://s3.amazonaws.com/connectly-storage/summary/meeting1_user1_summary.docx",
  "summary_image_url": "https://s3.amazonaws.com/connectly-storage/summary-image/mindmap.png",
  "subtitle_urls": {
    "en": "https://...subs_en.srt",
    "hi": "https://...subs_hi.srt",
    "te": "https://...subs_te.srt"
  }
}

GET /health

Check if the service is healthy:

curl http://localhost:8010/health


Response:

{"status": "healthy"}

🧪 Example Workflow

Upload your video:

curl -X POST "http://localhost:8010/upload/" \
     -F "meeting_id=meeting1" \
     -F "user_id=user1" \
     -F "file=@meeting.mp4"


Wait for processing to finish.

Get links to:

Captioned video

Multilingual subtitles

Transcript & Summary documents

Mind map image

🪶 Output Files
File	Description
captioned.mp4	Video with overlaid English subtitles
subs_en.srt / subs_hi.srt / subs_te.srt	Subtitles
transcript.docx	Full transcript text
summary.docx	AI-generated structured summary
mindmap.png	Visualization of summary topics
🔒 Security Notes

Replace any sensitive data (like <ip> or <password>) with placeholders.

Ensure your S3 bucket and MongoDB access credentials are protected.

Consider adding authentication or API keys to restrict API access.

🧰 Troubleshooting

Issue	Possible Cause	Fix
FFmpeg not found	Not installed or not in PATH	Install via sudo apt install ffmpeg
Whisper error	Wrong OpenAI key or model name	Verify OPENAI_API_KEY
S3 upload fails	Incorrect AWS credentials	Check environment vars
MongoDB auth failed	Wrong username/password	Update URI in code
Slow processing	Large videos / limited GPU	Reduce video length or chunk size
🧩 Future Improvements

Add async job queue (Celery / Redis)

Support for speaker diarization

Automatic language detection





🧑‍💻 Author

Developed by: Ajitha