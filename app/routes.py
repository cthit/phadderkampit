import os
import subprocess
import tempfile
import uuid
import random
import hashlib
from datetime import datetime
from urllib.parse import urlparse
from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
    jsonify,
)
from werkzeug.utils import secure_filename
import requests
from .extensions import db
from .models import SoundList, SoundClip, Team, Answer


def deterministic_shuffle(items, seed_source):
    """
    Shuffle a list deterministically based on a seed source.
    The same seed_source will always produce the same order.
    """
    if not items:
        return items

    # Create a deterministic seed from the source content
    # We'll use the IDs and titles of all clips to create a consistent hash
    seed_string = ""
    for item in items:
        seed_string += f"{item.id}:{item.title}:"

    # Create a hash from the seed string
    seed = int(hashlib.md5(seed_string.encode()).hexdigest()[:8], 16)

    # Create a copy and shuffle it with the deterministic seed
    shuffled_items = items.copy()
    random.seed(seed)
    random.shuffle(shuffled_items)

    return shuffled_items


bp = Blueprint("routes", __name__)


ALLOWED_EXTENSIONS = {"mp3", "wav", "ogg", "m4a", "flac"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_difficulty_mapping():
    """Load difficulty mapping from difficulties.txt file"""
    mapping = {"easy": [], "medium": [], "hard": []}

    try:
        with open("app/difficulties.txt", "r", encoding="utf-8") as f:
            current_category = None
            for line in f:
                line = line.strip()
                if line == "Lätta:":
                    current_category = "easy"
                elif line == "Mellan:":
                    current_category = "medium"
                elif line == "Svår:":
                    current_category = "hard"
                elif (
                    line
                    and current_category
                    and not line.startswith("✅")
                    and not line.startswith("❌")
                    and not line.startswith("☑️")
                ):
                    # Extract the title from the line (remove URLs and other info)
                    title = line.split(",")[0].split("https://")[0].strip()
                    if title:
                        mapping[current_category].append(title.lower())
    except FileNotFoundError:
        pass  # File doesn't exist, use empty mapping

    return mapping


def determine_difficulty(title):
    """Determine difficulty based on title using the mapping"""
    mapping = get_difficulty_mapping()
    title_lower = title.lower()

    # Normalize title by removing special characters and extra spaces
    title_normalized = " ".join(title_lower.replace("-", " ").replace("_", " ").split())

    # Check for exact matches first
    for difficulty, titles in mapping.items():
        for mapped_title in titles:
            # Normalize mapped title too
            mapped_normalized = " ".join(
                mapped_title.replace("-", " ").replace("_", " ").split()
            )

            if (
                mapped_normalized in title_normalized
                or title_normalized in mapped_normalized
            ):
                return difficulty

    # Check for partial matches with better word matching
    for difficulty, titles in mapping.items():
        for mapped_title in titles:
            # Normalize mapped title
            mapped_normalized = " ".join(
                mapped_title.replace("-", " ").replace("_", " ").split()
            )

            # Split into words and check for significant word matches
            title_words = set(title_normalized.split())
            mapped_words = set(mapped_normalized.split())

            # Check if any significant words match (longer than 3 chars)
            significant_matches = 0
            for title_word in title_words:
                if len(title_word) > 3:
                    for mapped_word in mapped_words:
                        if len(mapped_word) > 3:
                            if title_word in mapped_word or mapped_word in title_word:
                                significant_matches += 1

            # If we have at least 2 significant word matches, consider it a match
            if significant_matches >= 2:
                return difficulty

    # Check for single word matches for very specific terms
    for difficulty, titles in mapping.items():
        for mapped_title in titles:
            mapped_normalized = " ".join(
                mapped_title.replace("-", " ").replace("_", " ").split()
            )

            # For very specific terms like "widowmaker", "sekiro", "pokemon", etc.
            for word in title_normalized.split():
                if len(word) > 4:  # Longer words are more specific
                    for mapped_word in mapped_normalized.split():
                        if len(mapped_word) > 4:
                            if word == mapped_word:
                                return difficulty

    # Check for keyword matches (like "pokemon" in any context)
    # Use word boundaries to avoid false positives
    # Only use keywords for titles that don't match anything in difficulties.txt
    keywords = {
        "easy": [
            "minecraft",
            "roblox",
            "cs",
            "league",
            "valorant",
            "mario",
            "pac",
            "zelda",
        ],
        "medium": ["dark", "souls", "terraria", "portal", "tf2", "fortnite", "cod"],
        "hard": [
            "sekiro",
            "widowmaker",
            "warframe",
            "hearts",
            "iron",
            "eu4",
            "arma",
            "titanfall",
            "hollowknight",
            "kerbal",
        ],
        # Removed 'pokemon' from hard keywords since it's in difficulties.txt
    }

    # Split title into words for more precise matching
    title_words = set(title_normalized.split())

    for difficulty, keyword_list in keywords.items():
        for keyword in keyword_list:
            # Check if the keyword appears as a complete word
            if keyword in title_words:
                return difficulty

    # Default to medium if no match found
    return "medium"


@bp.route("/")
def index():
    lists = SoundList.query.order_by(SoundList.created_at.desc()).all()
    teams = Team.query.order_by(Team.created_at.desc()).all()
    return render_template("index.html", lists=lists, teams=teams)


@bp.route("/lists", methods=["POST"])
def create_list():
    name = request.form.get("name")
    description = request.form.get("description")
    if not name:
        flash("List name is required", "error")
        return redirect(url_for("routes.index"))
    lst = SoundList(name=name.strip(), description=description)
    db.session.add(lst)
    db.session.commit()
    flash("List created", "success")
    return redirect(url_for("routes.view_list", list_id=lst.id))


@bp.route("/lists/<int:list_id>")
def view_list(list_id):
    lst = SoundList.query.get_or_404(list_id)
    clips = (
        SoundClip.query.filter_by(list_id=list_id)
        .order_by(SoundClip.created_at.desc())
        .all()
    )

    # Apply deterministic shuffle to clips based on list content
    clips = deterministic_shuffle(clips, clips)

    teams = Team.query.order_by(Team.created_at.desc()).all()

    # Build a simple scoreboard: correct answers per team across this list
    team_scores = {t.id: 0 for t in teams}
    for clip in clips:
        for ans in clip.answers:
            if ans.is_correct:
                team_scores[ans.team_id] = team_scores.get(ans.team_id, 0) + 1

    return render_template(
        "list.html", lst=lst, clips=clips, teams=teams, team_scores=team_scores
    )


@bp.route("/teams", methods=["POST"])
def create_team():
    name = request.form.get("name")
    if not name:
        flash("Team name is required", "error")
        return redirect(request.referrer or url_for("routes.index"))
    team = Team(name=name.strip())
    db.session.add(team)
    db.session.commit()
    flash("Team created", "success")
    return redirect(request.referrer or url_for("routes.index"))


@bp.route("/clips/upload/<int:list_id>", methods=["POST"])
def upload_clip(list_id):
    lst = SoundList.query.get_or_404(list_id)
    files = request.files.getlist("files")
    title = request.form.get("title")
    description = request.form.get("description")

    if not files or all(file.filename == "" for file in files):
        flash("No files provided", "error")
        return redirect(url_for("routes.view_list", list_id=list_id))

    uploaded_count = 0
    error_count = 0

    for file in files:
        if file.filename == "":
            continue

        if not allowed_file(file.filename):
            flash(f"Unsupported file type: {file.filename}", "error")
            error_count += 1
            continue

        # Check for duplicate title in this list
        clip_title = title or file.filename
        existing_clip = SoundClip.query.filter_by(
            list_id=lst.id, title=clip_title
        ).first()

        if existing_clip:
            flash(
                f"Duplicate detected: '{clip_title}' already exists in this list",
                "error",
            )
            error_count += 1
            continue

        filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
        path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        file.save(path)

        # Determine difficulty based on title
        difficulty = determine_difficulty(clip_title)

        clip = SoundClip(
            list_id=lst.id,
            title=clip_title,
            description=description,
            filename=filename,
            original_name=file.filename,
            mime_type=file.mimetype,
            difficulty=difficulty,
        )
        db.session.add(clip)
        uploaded_count += 1

    if uploaded_count > 0:
        db.session.commit()
        if uploaded_count == 1:
            flash(f"1 clip uploaded (Difficulty: {difficulty.title()})", "success")
        else:
            flash(f"{uploaded_count} clips uploaded successfully", "success")

    if error_count > 0:
        flash(f"{error_count} files failed to upload", "error")

    return redirect(url_for("routes.view_list", list_id=list_id))


@bp.route("/clips/preview-url", methods=["POST"])
def preview_url():
    """Preview and convert audio from URL"""
    try:
        data = request.get_json()
        url = data.get("url")

        if not url:
            return jsonify({"error": "URL is required"}), 400

        # Download the audio file
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        # Get file info
        parsed_url = urlparse(url)
        original_filename = os.path.basename(parsed_url.path)
        if not original_filename:
            original_filename = "audio"

        # Create temporary file for original
        temp_dir = tempfile.gettempdir()
        temp_id = str(uuid.uuid4())

        # Determine file extension from content-type or URL
        content_type = response.headers.get("content-type", "").lower()
        if "ogg" in content_type or url.lower().endswith(".ogg"):
            original_ext = ".ogg"
        elif "mp3" in content_type or url.lower().endswith(".mp3"):
            original_ext = ".mp3"
        elif "wav" in content_type or url.lower().endswith(".wav"):
            original_ext = ".wav"
        elif "m4a" in content_type or url.lower().endswith(".m4a"):
            original_ext = ".m4a"
        else:
            original_ext = ".audio"  # fallback

        original_temp_path = os.path.join(temp_dir, f"original_{temp_id}{original_ext}")

        # Save downloaded file
        with open(original_temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        # Convert to MP3 using ffmpeg directly
        try:
            mp3_temp_path = os.path.join(temp_dir, f"converted_{temp_id}.mp3")

            # Use ffmpeg to convert and get info
            cmd = [
                "ffmpeg",
                "-i",
                original_temp_path,
                "-vn",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-f",
                "mp3",
                mp3_temp_path,
                "-y",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"FFmpeg conversion failed: {result.stderr}")

            # Get audio duration using ffprobe
            duration_cmd = [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                mp3_temp_path,
            ]
            duration_result = subprocess.run(
                duration_cmd, capture_output=True, text=True
            )
            duration = (
                float(duration_result.stdout.strip())
                if duration_result.returncode == 0
                else 0
            )

            # Create a URL for preview (serve the temp file)
            preview_url_path = f"/temp-audio/{temp_id}.mp3"

            # Create info file
            info_path = os.path.join(temp_dir, f"info_{temp_id}.txt")
            with open(info_path, "w") as f:
                f.write(
                    f"{mp3_temp_path}\n{original_filename}\n{original_ext[1:].upper()}"
                )

            # Calculate file info
            file_size = os.path.getsize(mp3_temp_path)
            size_str = (
                f"{file_size / 1024 / 1024:.1f}MB"
                if file_size > 1024 * 1024
                else f"{file_size / 1024:.1f}KB"
            )

            # Suggest title
            suggested_title = (
                original_filename.rsplit(".", 1)[0]
                if "." in original_filename
                else original_filename
            )

            return jsonify(
                {
                    "success": True,
                    "preview_url": preview_url_path,
                    "temp_file": temp_id,
                    "duration": f"{duration:.1f}",
                    "size": size_str,
                    "original_format": original_ext[1:].upper(),
                    "suggested_title": suggested_title,
                }
            )

        except Exception as e:
            # Clean up files
            if os.path.exists(original_temp_path):
                os.remove(original_temp_path)
            return jsonify({"error": f"Audio conversion failed: {str(e)}"}), 400

    except requests.RequestException as e:
        return jsonify({"error": f"Failed to download audio: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Preview failed: {str(e)}"}), 500


@bp.route("/temp-audio/<temp_id>")
def serve_temp_audio(temp_id):
    """Serve temporary audio files for preview"""
    temp_dir = tempfile.gettempdir()

    # Read temp file info
    info_path = os.path.join(temp_dir, f"info_{temp_id.replace('.mp3', '')}.txt")
    if not os.path.exists(info_path):
        return "Temp file not found", 404

    try:
        with open(info_path, "r") as f:
            mp3_path = f.readline().strip()

        if not os.path.exists(mp3_path):
            return "Audio file not found", 404

        return send_from_directory(temp_dir, os.path.basename(mp3_path))
    except Exception as e:
        return f"Error serving temp file: {str(e)}", 500


@bp.route("/clips/upload-url/<int:list_id>", methods=["POST"])
def upload_clip_from_url(list_id):
    """Upload clip from converted URL"""
    lst = SoundList.query.get_or_404(list_id)
    temp_file_id = request.form.get("converted_file")
    title = request.form.get("title")
    description = request.form.get("description")

    if not temp_file_id:
        flash("No converted file found", "error")
        return redirect(url_for("routes.view_list", list_id=list_id))

    temp_dir = tempfile.gettempdir()

    # Read temp file info
    info_path = os.path.join(temp_dir, f"info_{temp_file_id}.txt")
    if not os.path.exists(info_path):
        flash("Converted file expired", "error")
        return redirect(url_for("routes.view_list", list_id=list_id))

    try:
        with open(info_path, "r") as f:
            lines = f.readlines()
            mp3_path = lines[0].strip()
            original_filename = lines[1].strip()
            original_format = lines[2].strip()

        if not os.path.exists(mp3_path):
            flash("Converted file not found", "error")
            return redirect(url_for("routes.view_list", list_id=list_id))

        # Generate permanent filename
        filename = secure_filename(
            f"{datetime.utcnow().timestamp()}_{original_filename}.mp3"
        )
        permanent_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)

        # Move temp file to permanent location
        import shutil

        shutil.move(mp3_path, permanent_path)

        # Check for duplicate title in this list
        clip_title = title or original_filename
        existing_clip = SoundClip.query.filter_by(
            list_id=lst.id, title=clip_title
        ).first()

        if existing_clip:
            flash(
                f"Duplicate detected: '{clip_title}' already exists in this list",
                "error",
            )
            return redirect(url_for("routes.view_list", list_id=list_id))

        # Determine difficulty based on title
        difficulty = determine_difficulty(clip_title)

        # Create clip record
        clip = SoundClip(
            list_id=lst.id,
            title=clip_title,
            description=description,
            filename=filename,
            original_name=f"{original_filename} (converted from {original_format})",
            mime_type="audio/mpeg",
            difficulty=difficulty,
        )
        db.session.add(clip)
        db.session.commit()

        # Clean up temp files
        for temp_file in [
            info_path,
            os.path.join(temp_dir, f"original_{temp_file_id}.ogg"),
            os.path.join(temp_dir, f"original_{temp_file_id}.wav"),
            os.path.join(temp_dir, f"original_{temp_file_id}.m4a"),
        ]:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass

        flash(
            f"Audio converted and uploaded successfully (from {original_format})",
            "success",
        )
        return redirect(url_for("routes.view_list", list_id=list_id))

    except Exception as e:
        flash(f"Failed to save converted audio: {str(e)}", "error")
        return redirect(url_for("routes.view_list", list_id=list_id))


@bp.route("/uploads/<path:filename>")
def serve_upload(filename):
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    # Convert to absolute path to ensure it works regardless of working directory
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.abspath(upload_folder)

    file_path = os.path.join(upload_folder, filename)
    if not os.path.exists(file_path):
        current_app.logger.warning(f"Requested file not found: {filename}")
        return "File not found", 404

    return send_from_directory(upload_folder, filename)


@bp.route("/answers/batch", methods=["POST"])
def record_batch_answers():
    team_id = request.form.get("team_id", type=int)
    list_id = request.form.get("list_id", type=int)

    if not team_id:
        flash("Team is required for batch recording", "error")
        return redirect(request.referrer or url_for("routes.index"))

    team = Team.query.get_or_404(team_id)

    # Get all clips for this list
    if list_id:
        clips = SoundClip.query.filter_by(list_id=list_id).all()
    else:
        flash("List is required for batch recording", "error")
        return redirect(request.referrer or url_for("routes.index"))

    answers_recorded = 0
    answers_updated = 0

    # Process each clip's answer
    for clip in clips:
        # Form field names: clip_<clip_id>_result and clip_<clip_id>_notes
        result_field = f"clip_{clip.id}_result"
        notes_field = f"clip_{clip.id}_notes"

        result = request.form.get(result_field)
        notes = request.form.get(notes_field, "").strip()

        # Skip if no result provided for this clip
        if not result or result == "skip":
            continue

        is_correct = result == "correct"

        # Upsert-like behavior: one answer per team+clip
        existing = Answer.query.filter_by(team_id=team.id, clip_id=clip.id).first()
        if existing:
            existing.is_correct = is_correct
            existing.notes = (
                notes or existing.notes
            )  # Keep existing notes if new ones are empty
            existing.submitted_at = datetime.utcnow()
            answers_updated += 1
        else:
            ans = Answer(team=team, clip=clip, is_correct=is_correct, notes=notes)
            db.session.add(ans)
            answers_recorded += 1

    db.session.commit()

    # Provide feedback based on what was recorded
    if answers_recorded > 0 and answers_updated > 0:
        flash(
            f"Batch recording complete: {answers_recorded} new answers recorded, {answers_updated} answers updated for {team.name}",
            "success",
        )
    elif answers_recorded > 0:
        flash(
            f"Batch recording complete: {answers_recorded} answers recorded for {team.name}",
            "success",
        )
    elif answers_updated > 0:
        flash(
            f"Batch recording complete: {answers_updated} answers updated for {team.name}",
            "success",
        )
    else:
        flash(
            "No answers were recorded. Make sure to select results for the clips.",
            "warning",
        )

    return redirect(request.referrer or url_for("routes.index"))


@bp.route("/lists/<int:list_id>/delete", methods=["POST"])
def delete_list(list_id):
    """Delete an entire list and all its clips/answers"""
    lst = SoundList.query.get_or_404(list_id)

    # Delete associated files
    for clip in lst.clips:
        file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], clip.filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                current_app.logger.warning(
                    f"Failed to delete file {clip.filename}: {e}"
                )

    # Delete the list (cascade will handle clips and answers)
    db.session.delete(lst)
    db.session.commit()
    flash(f"List '{lst.name}' and all its content has been deleted", "success")
    return redirect(url_for("routes.index"))


@bp.route("/clips/<int:clip_id>/delete", methods=["POST"])
def delete_clip(clip_id):
    """Delete a single clip and its answers"""
    clip = SoundClip.query.get_or_404(clip_id)
    list_id = clip.list_id

    # Delete the physical file
    file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], clip.filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError as e:
            current_app.logger.warning(f"Failed to delete file {clip.filename}: {e}")

    # Delete the clip (cascade will handle answers)
    db.session.delete(clip)
    db.session.commit()
    flash(f"Clip '{clip.title}' has been deleted", "success")
    return redirect(url_for("routes.view_list", list_id=list_id))


@bp.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id):
    """Delete a team and all its answers"""
    team = Team.query.get_or_404(team_id)
    team_name = team.name

    # Delete the team (cascade will handle answers)
    db.session.delete(team)
    db.session.commit()
    flash(f"Team '{team_name}' and all its scores have been deleted", "success")
    return redirect(request.referrer or url_for("routes.index"))


@bp.route("/lists/<int:list_id>/clear-scores", methods=["POST"])
def clear_list_scores(list_id):
    """Clear all scores (answers) for a specific list"""
    lst = SoundList.query.get_or_404(list_id)

    # Delete all answers for clips in this list
    clip_ids = [clip.id for clip in lst.clips]
    if clip_ids:
        Answer.query.filter(Answer.clip_id.in_(clip_ids)).delete(
            synchronize_session=False
        )
        db.session.commit()
        flash(f"All scores for list '{lst.name}' have been cleared", "success")
    else:
        flash("No scores to clear", "info")

    return redirect(url_for("routes.view_list", list_id=list_id))


@bp.route("/teams/<int:team_id>/clear-scores", methods=["POST"])
def clear_team_scores(team_id):
    """Clear all scores for a specific team"""
    team = Team.query.get_or_404(team_id)

    # Delete all answers for this team
    Answer.query.filter_by(team_id=team_id).delete()
    db.session.commit()
    flash(f"All scores for team '{team.name}' have been cleared", "success")
    return redirect(request.referrer or url_for("routes.index"))


@bp.route("/lists/<int:list_id>/teams/<int:team_id>/clear-scores", methods=["POST"])
def clear_team_list_scores(list_id, team_id):
    """Clear scores for a specific team on a specific list"""
    lst = SoundList.query.get_or_404(list_id)
    team = Team.query.get_or_404(team_id)

    # Delete answers for this team on clips in this list
    clip_ids = [clip.id for clip in lst.clips]
    if clip_ids:
        Answer.query.filter(
            Answer.team_id == team_id, Answer.clip_id.in_(clip_ids)
        ).delete(synchronize_session=False)
        db.session.commit()
        flash(
            f"Scores for team '{team.name}' on list '{lst.name}' have been cleared",
            "success",
        )
    else:
        flash("No scores to clear", "info")

    return redirect(url_for("routes.view_list", list_id=list_id))


@bp.route("/scoreboard")
def scoreboard():
    """Overall scoreboard showing teams ranked by total correct answers"""
    teams = Team.query.all()

    # Calculate scores for each team across all lists
    team_stats = []
    for team in teams:
        total_correct = Answer.query.filter_by(team_id=team.id, is_correct=True).count()
        total_incorrect = Answer.query.filter_by(
            team_id=team.id, is_correct=False
        ).count()
        total_answered = total_correct + total_incorrect

        # Calculate accuracy percentage
        accuracy = (total_correct / total_answered * 100) if total_answered > 0 else 0

        team_stats.append(
            {
                "team": team,
                "total_correct": total_correct,
                "total_incorrect": total_incorrect,
                "total_answered": total_answered,
                "accuracy": accuracy,
            }
        )

    # Sort by total correct answers (descending), then by accuracy
    team_stats.sort(key=lambda x: (x["total_correct"], x["accuracy"]), reverse=True)

    # Add ranking
    for i, stats in enumerate(team_stats):
        stats["rank"] = i + 1

    # Get list-specific scoreboards as well
    lists = SoundList.query.all()
    list_scoreboards = {}

    for lst in lists:
        # Get clips for this list
        clip_ids = [clip.id for clip in lst.clips]
        if not clip_ids:
            continue

        list_team_stats = []
        for team in teams:
            list_correct = Answer.query.filter(
                Answer.team_id == team.id,
                Answer.clip_id.in_(clip_ids),
                Answer.is_correct == True,
            ).count()

            list_incorrect = Answer.query.filter(
                Answer.team_id == team.id,
                Answer.clip_id.in_(clip_ids),
                Answer.is_correct == False,
            ).count()

            list_answered = list_correct + list_incorrect
            list_accuracy = (
                (list_correct / list_answered * 100) if list_answered > 0 else 0
            )

            if list_answered > 0:  # Only include teams that have answered questions
                list_team_stats.append(
                    {
                        "team": team,
                        "correct": list_correct,
                        "incorrect": list_incorrect,
                        "answered": list_answered,
                        "accuracy": list_accuracy,
                        "total_clips": len(clip_ids),
                    }
                )

        # Sort by correct answers, then by accuracy
        list_team_stats.sort(key=lambda x: (x["correct"], x["accuracy"]), reverse=True)

        # Add ranking
        for i, stats in enumerate(list_team_stats):
            stats["rank"] = i + 1

        list_scoreboards[lst.id] = {"list": lst, "teams": list_team_stats}

    return render_template(
        "scoreboard.html", overall_teams=team_stats, list_scoreboards=list_scoreboards
    )


@bp.route("/scoreboard/list/<int:list_id>")
def list_scoreboard(list_id):
    """Detailed scoreboard for a specific list"""
    lst = SoundList.query.get_or_404(list_id)
    clips = (
        SoundClip.query.filter_by(list_id=list_id)
        .order_by(SoundClip.created_at.desc())
        .all()
    )

    # Apply deterministic shuffle to clips based on list content
    clips = deterministic_shuffle(clips, clips)

    teams = Team.query.all()

    # Build detailed scoreboard with per-clip results
    team_results = []
    for team in teams:
        team_data = {
            "team": team,
            "clip_results": {},
            "total_correct": 0,
            "total_incorrect": 0,
            "total_answered": 0,
        }

        # Get all answers for this team on this list
        answers = Answer.query.filter(
            Answer.team_id == team.id, Answer.clip_id.in_([clip.id for clip in clips])
        ).all()

        # Create a lookup for answers by clip_id
        answer_lookup = {answer.clip_id: answer for answer in answers}

        # Process each clip
        for clip in clips:
            if clip.id in answer_lookup:
                answer = answer_lookup[clip.id]
                team_data["clip_results"][clip.id] = {
                    "is_correct": answer.is_correct,
                    "notes": answer.notes,
                    "submitted_at": answer.submitted_at,
                }
                if answer.is_correct:
                    team_data["total_correct"] += 1
                else:
                    team_data["total_incorrect"] += 1
                team_data["total_answered"] += 1
            else:
                team_data["clip_results"][clip.id] = None  # Not answered

        # Calculate accuracy
        team_data["accuracy"] = (
            team_data["total_correct"] / team_data["total_answered"] * 100
            if team_data["total_answered"] > 0
            else 0
        )

        team_results.append(team_data)

    # Sort teams by score (correct answers), then by accuracy
    team_results.sort(key=lambda x: (x["total_correct"], x["accuracy"]), reverse=True)

    # Add ranking
    for i, team_data in enumerate(team_results):
        team_data["rank"] = i + 1

    return render_template(
        "list_scoreboard.html", lst=lst, clips=clips, team_results=team_results
    )

@bp.route("/clips/<int:clip_id>/edit", methods=["POST"])
def edit_clip(clip_id):
    """Update title, description, and difficulty for a clip"""
    clip = SoundClip.query.get_or_404(clip_id)
    
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    difficulty = request.form.get("difficulty", "medium").strip().lower()

    if not title:
        flash("Title is required", "error")
        return redirect(url_for("routes.view_list", list_id=clip.list_id))

    if difficulty not in ["easy", "medium", "hard"]:
        difficulty = "medium"

    # Check for duplicate titles within the list
    existing = SoundClip.query.filter(
        SoundClip.list_id == clip.list_id,
        SoundClip.title == title,
        SoundClip.id != clip.id
    ).first()

    if existing:
        flash(f"Duplicate detected: '{title}' already exists in this list", "error")
        return redirect(url_for("routes.view_list", list_id=clip.list_id))

    clip.title = title
    clip.description = description if description else None
    clip.difficulty = difficulty

    db.session.commit()
    flash(f"Clip '{clip.title}' updated successfully", "success")
    return redirect(url_for("routes.view_list", list_id=clip.list_id))