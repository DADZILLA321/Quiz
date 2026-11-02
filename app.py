import io
import json
import os
import random
import re
import time

from flask import Flask, jsonify, request, send_from_directory
from google import genai
from google.genai import types
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024        # Vercel allows about 4.5 MB per request
if os.getenv("VERCEL"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]    # main model, then a quick backup
MAX_CHARS = 30000                                         # how much of the notes we send to Gemini
MIN_CHARS = 80
HOURLY_LIMIT = 12                                         # quizzes per visitor per hour (best effort)

LEVELS = {
    "easy": "Easy: direct facts, definitions and simple recall. The answer is clearly written in the notes.",
    "medium": "Medium: check understanding. Ask how and why, compare ideas, and use the concepts in small examples.",
    "hard": "Hard: deeper thinking. Ask for analysis, connecting several ideas, application to new situations, "
            "and tricky cases. For multiple choice, make the wrong options close and believable.",
}
LANGUAGES = {
    "English": "English",
    "Hindi": "Hindi (Devanagari script)",
    "Hinglish": "Hinglish (Hindi written in English letters)",
}

_client = None
_visits = {}


def get_client():
    global _client
    if _client is None:
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set on the server.")
        _client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=25000))
    return _client


def too_many_requests(ip):
    now = time.time()
    recent = [t for t in _visits.get(ip, []) if now - t < 3600]
    if len(recent) >= HOURLY_LIMIT:
        _visits[ip] = recent
        return True
    recent.append(now)
    _visits[ip] = recent
    return False


def read_file(f):
    name = (f.filename or "").lower()
    data = f.read()
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="ignore")
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages[:80]
        return "\n".join((p.extract_text() or "") for p in pages)
    if name.endswith(".docx"):
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    raise ValueError("Please upload a .txt, .md, .pdf or .docx file.")


def build_prompt(notes, level, qtype, count, language):
    if qtype == "mcq":
        mix = f"Write exactly {count} multiple choice questions."
    elif qtype == "short":
        mix = f"Write exactly {count} short answer questions."
    else:
        mcq = (count + 1) // 2
        mix = f"Write exactly {count} questions: {mcq} multiple choice and {count - mcq} short answer."
    return f"""You are a teacher who writes practice questions from a student's notes.

{mix}
Difficulty: {LEVELS[level]}
Language of the questions and answers: {LANGUAGES[language]}

Rules:
- Use only what is in the notes. Do not invent facts.
- Cover different parts of the notes and do not repeat a question.
- Multiple choice: exactly 4 options, only one correct. Do not write "A)" or "1." in front of options.
- Short answer: the answer must be 1 to 3 sentences.
- Give a one line explanation for every question.

Return only JSON in exactly this shape:
{{"questions": [
  {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "answer_index": 0, "explanation": "..."}},
  {{"type": "short", "question": "...", "answer": "...", "explanation": "..."}}
]}}

NOTES:
\"\"\"
{notes}
\"\"\"
"""


def ask_gemini(prompt):
    last = None
    for model in MODELS:
        for fast in (True, False):                       # try with low thinking first, then without
            try:
                kwargs = {"response_mime_type": "application/json", "temperature": 0.7}
                if fast:
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
                reply = get_client().models.generate_content(
                    model=model, contents=prompt, config=types.GenerateContentConfig(**kwargs))
                if reply.text:
                    return reply.text
            except Exception as e:
                last = e
                print(f"[quiz] {model} failed: {str(e)[:150]}")
                if fast and "thinking" in str(e).lower():
                    continue
                break
    raise last or RuntimeError("No reply from Gemini.")


def clean_questions(text, qtype, count):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    items = data.get("questions", []) if isinstance(data, dict) else data
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        question = str(item.get("question", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if not question or kind not in ("mcq", "short"):
            continue
        if qtype != "both" and kind != qtype:
            continue
        if kind == "mcq":
            options = [str(o).strip() for o in item.get("options", []) if str(o).strip()]
            index = item.get("answer_index")
            if len(options) != 4 or not isinstance(index, int) or not 0 <= index < 4:
                continue
            right = options[index]
            random.shuffle(options)                      # so the answer is not always in the same place
            out.append({"type": "mcq", "question": question, "options": options,
                        "answer_index": options.index(right), "explanation": explanation})
        else:
            answer = str(item.get("answer", "")).strip()
            if answer:
                out.append({"type": "short", "question": question, "answer": answer, "explanation": explanation})
    return out[:count]


@app.route("/")
def home():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, key_set=bool((os.getenv("GEMINI_API_KEY") or "").strip()))


@app.errorhandler(413)
def too_big(e):
    return jsonify(error="That file is too big. Please keep it under 4 MB."), 413


@app.post("/api/generate")
def generate():
    if too_many_requests(request.remote_addr or "x"):
        return jsonify(error="You have made many quizzes in the last hour. Please try again a little later."), 429

    level = request.form.get("difficulty", "medium")
    qtype = request.form.get("qtype", "both")
    language = request.form.get("language", "English")
    try:
        count = max(3, min(20, int(request.form.get("count", 10))))
    except ValueError:
        count = 10
    if level not in LEVELS or qtype not in ("mcq", "short", "both") or language not in LANGUAGES:
        return jsonify(error="Please check your options and try again."), 400

    notes = (request.form.get("text") or "").strip()
    upload = request.files.get("file")
    if upload and upload.filename:
        try:
            notes = (notes + "\n" + read_file(upload)).strip()
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception:
            return jsonify(error="Could not read that file. Try pasting the text instead."), 400

    notes = re.sub(r"\n{3,}", "\n\n", notes)
    if len(notes) < MIN_CHARS:
        return jsonify(error="Your notes are too short. Please add a few more lines."), 400
    notes = notes[:MAX_CHARS]

    try:
        text = ask_gemini(build_prompt(notes, level, qtype, count, language))
        questions = clean_questions(text, qtype, count)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except Exception as e:
        busy = any(k in str(e).lower() for k in ("429", "503", "overloaded", "unavailable", "quota"))
        msg = "The AI is busy right now. Please try again in a minute." if busy else "Could not make the quiz. Please try again."
        return jsonify(error=msg), 503 if busy else 500

    if not questions:
        return jsonify(error="Could not make good questions from these notes. Try adding more detail."), 422
    return jsonify(questions=questions, difficulty=level, qtype=qtype)


if __name__ == "__main__":
    app.run(debug=True)
