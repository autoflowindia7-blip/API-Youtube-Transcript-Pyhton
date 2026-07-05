
import logging
import re
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    RequestBlocked,
    CouldNotRetrieveTranscript
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app)


def clean_transcript_text(text):
    """
    Clean transcript text for optimal use with LLMs (Gemini, OpenAI).
    
    Cleaning steps:
    1. Remove music symbols (♪, ♫)
    2. Remove common non-speech annotations ([Music], [Applause], [Laughter])
    3. Replace line breaks with spaces
    4. Remove duplicate spaces
    5. Preserve all punctuation
    
    Args:
        text (str): Original transcript text
        
    Returns:
        str: Cleaned transcript text
    """
    # Remove music symbols
    cleaned = re.sub(r'[♪♫]', '', text)
    
    # Remove common non-speech annotations (case-insensitive)
    cleaned = re.sub(r'\[(Music|Applause|Laughter)\]', '', cleaned, flags=re.IGNORECASE)
    
    # Replace line breaks with spaces
    cleaned = cleaned.replace('\n', ' ')
    
    # Remove duplicate spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned


def merge_into_chunks(segments, chunk_duration=30):
    """
    Merge transcript segments into chunks of specified duration.
    
    Args:
        segments (list): List of transcript segments from youtube-transcript-api
        chunk_duration (int): Target duration for each chunk in seconds (default: 30)
        
    Returns:
        list: List of merged chunks with start, end, timestamp, and text
    """
    chunks = []
    current_chunk = None
    
    for segment in segments:
        # Clean the segment text
        cleaned_text = clean_transcript_text(segment["text"])
        
        # Skip empty segments after cleaning
        if not cleaned_text:
            continue
            
        # Determine which 30-second window this segment belongs to
        chunk_start = int(segment["start"] // chunk_duration) * chunk_duration
        chunk_end = chunk_start + chunk_duration
        
        # Format timestamp as MM:SS-MM:SS
        def _format_ts(secs):
            m = secs // 60
            s = secs % 60
            return f"{m:02d}:{s:02d}"
        timestamp = f"{_format_ts(chunk_start)}-{_format_ts(chunk_end)}"
        
        # Create new chunk or append to existing
        if current_chunk is None or current_chunk["start"] != chunk_start:
            if current_chunk is not None:
                chunks.append(current_chunk)
            current_chunk = {
                "start": chunk_start,
                "end": chunk_end,
                "timestamp": timestamp,
                "text": cleaned_text
            }
        else:
            current_chunk["text"] += f" {cleaned_text}"
    
    # Add final chunk
    if current_chunk is not None:
        chunks.append(current_chunk)
    
    return chunks


def get_available_transcript(video_id):
    
    # Webshare proxy to bypass Railway IP block
    proxy_url = "http://cwlplckt:gb615m36qqbd@31.59.20.176:6754/"
    
    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }
    
    api = YouTubeTranscriptApi(proxies=proxies)
    transcript_list = api.list(video_id)
    
    try:
        return transcript_list.find_transcript(["en", "en-US"]).fetch(), "en"
    except NoTranscriptFound:
        pass
        
    try:
        return transcript_list.find_transcript(["hi"]).fetch(), "hi"
    except NoTranscriptFound:
        pass
        
    for transcript in transcript_list:
        return transcript.fetch(), transcript.language_code
        
    raise NoTranscriptFound(f"No transcripts found for video {video_id}")


@app.route("/", methods=["GET"])
def home():
    """Home endpoint with API usage information."""
    return jsonify({
        "success": True,
        "message": "YouTube Transcript API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "transcript": "/transcript?video_id=VIDEO_ID"
        }
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint to verify server is operational."""
    logger.info("Health check requested")
    return jsonify({
        "success": True,
        "status": "running"
    })


@app.route("/transcript", methods=["GET"])
def transcript():
    """
    Get YouTube video transcript as clean 30-second chunks.
    
    Query Parameters:
        video_id (str): YouTube video ID (required)
        
    Returns:
        JSON: Transcript chunks with metadata
    """
    video_id = request.args.get("video_id")
    
    # Validate input
    if not video_id:
        logger.warning("Missing video_id parameter")
        return jsonify({
            "success": False,
            "error": "Missing video_id parameter"
        }), 400
        
    logger.info(f"Processing transcript request for video: {video_id}")
    
    try:
        # Get best available transcript
        fetched, language = get_available_transcript(video_id)
        logger.info(f"Retrieved transcript in language: {language}")
        
        # Convert to list of dicts for processing
        segments = []
        for item in fetched:
            segments.append({
                "text": item.text,
                "start": item.start,
                "duration": item.duration
            })
        
        # Merge into 30-second chunks
        chunks = merge_into_chunks(segments)
        logger.info(f"Successfully processed {len(chunks)} chunks for video {video_id}")
        
        return jsonify({
            "success": True,
            "videoId": video_id,
            "language": language,
            "totalChunks": len(chunks),
            "chunks": chunks
        })
        
    except VideoUnavailable:
        logger.error(f"Video unavailable: {video_id}")
        return jsonify({
            "success": False,
            "error": "Invalid video ID or video unavailable"
        }), 404
    except TranscriptsDisabled:
        logger.error(f"Transcripts disabled for video: {video_id}")
        return jsonify({
            "success": False,
            "error": "Transcripts are disabled for this video"
        }), 403
    except NoTranscriptFound:
        logger.error(f"No transcript found for video: {video_id}")
        return jsonify({
            "success": False,
            "error": "No transcript available for this video"
        }), 404
    except RequestBlocked:
        logger.error(f"Request blocked (rate limit/IP ban) for video: {video_id}")
        return jsonify({
            "success": False,
            "error": "Request blocked. Please try again later or use a different IP."
        }), 429
    except CouldNotRetrieveTranscript as e:
        logger.error(f"Could not retrieve transcript: {str(e)}")
        return jsonify({
            "success": False,
            "error": "Could not retrieve transcript. Please check your connection and try again."
        }), 503
    except Exception as e:
        import traceback
        traceback_str = traceback.format_exc()
        logger.error(f"Unexpected error processing transcript: {str(e)}\n{traceback_str}")
        return jsonify({
            "success": False,
            "error": "An unexpected error occurred"
        }), 500


if __name__ == "__main__":
    # Get port from environment variable for Railway deployment, use 5000 as default
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    
    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug
    )

