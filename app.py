import os
import random
import time
import datetime
import json
from datetime import timedelta
from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import requests
import re # For template parsing
from urllib.parse import urlparse

# --- Configuration ---
app = Flask(__name__)

# Database configuration: using a local SQLite file
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///scheduler.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'a_very_secret_key_for_session_management' 

# File upload configuration
UPLOAD_FOLDER = 'media'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 Megabytes total limit
MAX_FILE_SIZE_MB = 10 # Individual file size limit

db = SQLAlchemy(app)

# --- Database Models ---

class Page(db.Model):
    """Stores credentials and settings for each Facebook Page."""
    id = db.Column(db.Integer, primary_key=True)
    page_name = db.Column(db.String(255), nullable=False)
    page_id = db.Column(db.String(100), unique=True, nullable=False)
    access_token = db.Column(db.String(500), nullable=False)
    
    # Scheduling settings (13 time slots)
    time_slots = db.Column(db.String(500), default='08:00,09:00,10:00,11:00,12:00,13:00,14:00,15:00,16:00,17:00,18:00,19:00,20:00')
    
    # Content restrictions
    allow_images = db.Column(db.Boolean, default=True)
    allow_videos = db.Column(db.Boolean, default=True)

class MediaFolder(db.Model):
    """Organizes media files into user-defined folders."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

class MediaFile(db.Model):
    """Stores metadata for uploaded images/videos."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    original_name = db.Column(db.String(255))
    file_type = db.Column(db.String(50)) # e.g., 'image/jpeg', 'video/mp4'
    upload_date = db.Column(db.DateTime, default=datetime.datetime.now)
    
    # Link to folder
    folder_id = db.Column(db.Integer, db.ForeignKey('media_folder.id'), nullable=True)
    folder = db.relationship('MediaFolder', backref='files')

class ScheduledPost(db.Model):
    """Stores details for pending and completed posts."""
    id = db.Column(db.Integer, primary_key=True)
    
    # Relationships
    page_id = db.Column(db.Integer, db.ForeignKey('page.id'), nullable=False)
    page = db.relationship('Page', backref='scheduled_posts')
    media_file_id = db.Column(db.Integer, db.ForeignKey('media_file.id'), nullable=True)
    media_file = db.relationship('MediaFile', backref='schedules')
    
    # Post details
    title = db.Column(db.String(255))
    description = db.Column(db.Text)
    scheduled_time = db.Column(db.Integer) # UNIX timestamp
    media_type = db.Column(db.String(10)) # 'image' or 'video'
    
    # Status: 'scheduled', 'posted', 'failed'
    status = db.Column(db.String(10), default='scheduled')
    # Control execution: True means worker can process, False means skip (Pause/Disable)
    is_active = db.Column(db.Boolean, default=True) 
    
    fb_post_id = db.Column(db.String(100), nullable=True)
    error_message = db.Column(db.Text, nullable=True)


# --- Initialization ---

def create_initial_db_entries():
    """Initializes the database, creating the tables and a default folder."""
    with app.app_context():
        db.create_all()

        if MediaFolder.query.count() == 0:
            default_folder = MediaFolder(name='Default Folder')
            db.session.add(default_folder)
            db.session.commit()

create_initial_db_entries()

# --- Worker & Helper Functions ---

def generate_post_content(title_template, description_template):
    """Generates dynamic content using [HH:MM] and [RND3]."""
    now = datetime.datetime.now()
    hour_minute = now.strftime("%H:%M")
    random_3_digit = str(random.randint(100, 999))
    title = title_template.replace('[HH:MM]', hour_minute).replace('[RND3]', random_3_digit)
    description = description_template
    return title, description

def get_next_available_slot(page_id, time_slots_str, start_dt):
    """Finds the next unique time slot for scheduling."""
    time_slots = sorted([ts.strip() for ts in time_slots_str.split(',') if ts.strip()])
    if not time_slots: return None
    start_dt += timedelta(minutes=1)
    current_dt = start_dt.replace(second=0, microsecond=0)
    search_limit = current_dt + timedelta(days=7)
    
    while current_dt < search_limit:
        time_slot_str = current_dt.strftime("%H:%M")
        
        if time_slot_str in time_slots:
            scheduled_unix = int(current_dt.timestamp())
            existing_post = ScheduledPost.query.filter(
                ScheduledPost.page_id == page_id,
                ScheduledPost.scheduled_time == scheduled_unix,
                ScheduledPost.status != 'failed'
            ).first()

            if not existing_post:
                return current_dt
        
        current_dt += timedelta(minutes=1)

        if current_dt.minute == 0:
            if current_dt.hour == 0:
                h, m = map(int, time_slots[0].split(':'))
                current_dt = current_dt.replace(hour=h, minute=m, day=current_dt.day + 1)
            else:
                pass
    return None

def check_token_and_get_page_info(page_id, access_token):
    """Checks token validity against FB API and fetches page name."""
    if not page_id or not access_token:
        return {'is_valid': False, 'page_name': None}
    
    GRAPH_API_URL = f"https://graph.facebook.com/v20.0/{page_id}"
    params = {'fields': 'name', 'access_token': access_token}
    
    try:
        response = requests.get(GRAPH_API_URL, params=params, timeout=5) 
        response.raise_for_status()
        data = response.json()
        
        if 'name' in data:
            return {'is_valid': True, 'page_name': data['name']}
        else:
            return {'is_valid': False, 'page_name': 'ID/Token Invalid or Unauthorized'}

    except requests.exceptions.HTTPError as e:
        try:
            error_data = response.json()
            error_message = error_data.get('error', {}).get('message', 'API Error')
            return {'is_valid': False, 'page_name': f'Error: {error_message}'}
        except:
             return {'is_valid': False, 'page_name': 'Connection Failed (Check Token/Permissions)'}

    except requests.exceptions.RequestException:
        return {'is_valid': False, 'page_name': 'Connection Timeout/Error'}

def post_to_facebook(post_id, publish_now=False):
    """
    Core function to execute a post using the Direct File Upload method.
    """
    post = ScheduledPost.query.get(post_id)
    if not post or not post.media_file or not post.page:
        app.logger.error(f"Worker Error: Post ID {post_id} missing relationship data.")
        return False, "Post, Media, or Page not found."

    page = post.page
    media = post.media_file
    
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
    
    if not os.path.exists(file_path):
        app.logger.error(f"Worker Error: Media file not found on disk: {file_path}")
        return False, "Media file missing from server disk."
    
    is_video = 'video' in media.file_type.lower()
    
    # Setup API Edge
    if is_video:
        API_EDGE = f"/{page.page_id}/videos"
        data_params = {
            'description': f"{post.title}\n\n{post.description}",
            'access_token': page.access_token,
        }
        if not page.allow_videos:
            return False, f"Page '{page.page_name}' restricted: Videos not allowed."
    else:
        API_EDGE = f"/{page.page_id}/photos"
        data_params = {
            'caption': f"{post.title}\n\n{post.description}",
            'access_token': page.access_token,
        }
        if not page.allow_images:
            return False, f"Page '{page.page_name}' restricted: Images not allowed."

    # --- FIX START: Smart Scheduling Logic ---
    # If the worker picked this up, the scheduled time has likely passed or is right now.
    # We check if the time is in the past. If so, we force publish_now.
    current_unix_time = int(time.time())
    
    # If the user clicked "Post Now" OR the scheduled time has arrived/passed
    should_publish_immediately = publish_now or (post.scheduled_time <= current_unix_time + 60) # 60s buffer

    if should_publish_immediately:
        # PUBLISH NOW: Do not send scheduled_publish_time. Defaults to published=true
        app.logger.info(f"Post ID {post_id}: Time arrived (or forced). Publishing immediately.")
        pass 
    else:
        # FUTURE SCHEDULE: Send to FB to hold until later
        # Note: FB requires schedule to be at least 10 mins in future, usually.
        # If you use this tool's worker, this block is rarely hit, but good for safety.
        data_params['scheduled_publish_time'] = post.scheduled_time
        data_params['published'] = 'false'
        app.logger.info(f"Post ID {post_id}: Sending to FB to schedule for {post.scheduled_time}")
    # --- FIX END ---

    GRAPH_API_URL = f"https://graph.facebook.com/v20.0{API_EDGE}"
    
    try:
        with open(file_path, 'rb') as f:
            files = {'source': (media.filename, f, media.file_type)}
            
            response = requests.post(GRAPH_API_URL, data=data_params, files=files, timeout=60) 
            response_json = response.json()
            
            if 'id' in response_json:
                post.status = 'posted'
                post.fb_post_id = response_json['id']
                # Ensure the DB reflects the actual publish time
                if should_publish_immediately:
                    post.scheduled_time = int(time.time()) 
                db.session.commit()
                return True, response_json['id']
            else:
                error_msg = response_json.get('error', {}).get('message', 'Unknown API Error')
                app.logger.error(f"FB API Error for ID {post_id}: {error_msg}")
                return False, error_msg
                
    except Exception as e:
        error_message = f"Internal Worker Error: {str(e)}"
        app.logger.error(error_message)
        post.status = 'failed'
        post.error_message = error_message
        db.session.commit()
        return False, error_message


# --- Routes ---

@app.route('/')
def index():
    """Serves the main application dashboard."""
    return render_template('index.html')

@app.route('/media/<filename>')
def uploaded_file(filename):
    """Serves media files from the UPLOAD_FOLDER."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# --- API: Page Management ---

@app.route('/api/pages', methods=['GET', 'POST'])
def api_pages():
    if request.method == 'POST':
        data = request.get_json()
        if not all(k in data for k in ['name', 'id', 'token']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        status = check_token_and_get_page_info(data['id'], data['token'])
        if not status['is_valid']:
            return jsonify({'error': status['page_name']}), 400
            
        new_page = Page(
            page_name=data['name'],
            page_id=data['id'],
            access_token=data['token'],
            time_slots=data.get('slots', Page.time_slots.default),
            allow_images=data.get('allowImages', True),
            allow_videos=data.get('allowVideos', True)
        )
        try:
            db.session.add(new_page)
            db.session.commit()
            return jsonify({'message': 'Page added successfully', 'page': {'id': new_page.id, 'name': new_page.page_name}}), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Page ID already exists or DB error: {str(e)}'}), 400

    
    # GET method logic
    try:
        pages = Page.query.all()
        page_data = []
        for page in pages:
            status = check_token_and_get_page_info(page.page_id, page.access_token)
            page_data.append({
                'id': page.id,
                'page_name': status['page_name'] if not status['is_valid'] else page.page_name,
                'page_id': page.page_id,
                'access_token': page.access_token,
                'time_slots': page.time_slots,
                'allow_images': page.allow_images,
                'allow_videos': page.allow_videos,
                'is_valid': status['is_valid']
            })
        return jsonify(page_data)
    
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Critical error loading pages: {e}")
        return jsonify({'error': 'Internal server error while retrieving page list. Check server logs for details.'}), 500


@app.route('/api/pages/<int:page_id>', methods=['DELETE'])
def delete_page(page_id):
    page = Page.query.get(page_id)
    if not page:
        return jsonify({'error': 'Page not found'}), 404
        
    ScheduledPost.query.filter_by(page_id=page_id).delete()
    db.session.delete(page)
    db.session.commit()
    return jsonify({'message': 'Page and related schedules deleted successfully'}), 200

# --- API: Folder Management ---

@app.route('/api/folders', methods=['GET', 'POST'])
def api_folders():
    if request.method == 'POST':
        data = request.get_json()
        folder_name = data.get('name')
        if not folder_name:
            return jsonify({'error': 'Folder name required'}), 400
        
        if MediaFolder.query.filter_by(name=folder_name).first():
            return jsonify({'error': 'Folder name already exists'}), 400
            
        new_folder = MediaFolder(name=folder_name)
        db.session.add(new_folder)
        db.session.commit()
        return jsonify({'message': 'Folder created successfully', 'id': new_folder.id, 'name': new_folder.name}), 201

    folders = MediaFolder.query.all()
    return jsonify([{'id': f.id, 'name': f.name, 'file_count': len(f.files)} for f in folders])

# --- API: Media Management ---

@app.route('/api/upload_media', methods=['POST'])
def api_upload_media():
    folder_id_str = request.form.get('folderId')
    if not folder_id_str:
        return jsonify({'error': 'Folder ID is required for upload.'}), 400

    if 'mediaFiles' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400
        
    files = request.files.getlist('mediaFiles')
    uploaded_files = []
    
    try:
        folder_id = int(folder_id_str)
    except ValueError:
        return jsonify({'error': 'Invalid folder ID format.'}), 400
    
    for file in files:
        if file.filename == '':
            continue
            
        filename = secure_filename(file.filename)
        unique_filename = f"{int(time.time())}_{random.randint(1000, 9999)}_{filename}"

        try:
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                print(f"File {filename} too large ({file_size} bytes)")
                continue

            file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(file_path)
            
            new_media = MediaFile(
                filename=unique_filename,
                original_name=file.filename,
                file_type=file.content_type,
                folder_id=folder_id
            )
            db.session.add(new_media)
            db.session.commit()
            uploaded_files.append({
                'id': new_media.id,
                'name': new_media.original_name,
                'type': new_media.file_type
            })
            
        except Exception as e:
            app.logger.error(f"Error during file upload: {e}")
            if 'file_path' in locals() and os.path.exists(file_path):
                os.remove(file_path)
            db.session.rollback()
            return jsonify({'error': f'Failed to process file {filename}.'}), 500

    return jsonify({'message': f'{len(uploaded_files)} files uploaded successfully.', 'files': uploaded_files}), 200

@app.route('/api/media', methods=['GET'])
def api_media():
    folder_id_str = request.args.get('folderId')
    
    if not folder_id_str:
        return jsonify([]), 200
    
    try:
        folder_id = int(folder_id_str)
    except ValueError:
        return jsonify({'error': 'Invalid folder ID format.'}), 400

    media_files = MediaFile.query.filter_by(folder_id=folder_id).order_by(MediaFile.upload_date.desc()).all()
    
    data = []
    for media in media_files:
        data.append({
            'id': media.id,
            'name': media.original_name,
            'type': media.file_type,
            'filename': media.filename
        })
    return jsonify(data)

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    media = MediaFile.query.get(media_id)
    if not media:
        return jsonify({'error': 'Media not found'}), 404
        
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    ScheduledPost.query.filter_by(media_file_id=media_id).delete()
    
    db.session.delete(media)
    db.session.commit()
    return jsonify({'message': 'Media and related schedules deleted successfully'}), 200


# --- API: Scheduling and Automation ---

@app.route('/api/schedule_automation', methods=['POST'])
def api_schedule_automation():
    data = request.get_json()
    media_ids = data.get('mediaIds', [])
    page_ids = data.get('pageIds', [])
    title_template = data.get('titleTemplate', '')
    description_template = data.get('descriptionTemplate', '')
    
    if not media_ids or not page_ids:
        return jsonify({'error': 'No media or pages selected for scheduling.'}), 400
        
    media_files = MediaFile.query.filter(MediaFile.id.in_(media_ids)).all()
    target_pages = Page.query.filter(Page.id.in_(page_ids)).all()
    
    if not media_files or not target_pages:
        return jsonify({'error': 'Selected media or pages not found.'}), 404

    scheduled_count = 0
    
    last_scheduled_time = datetime.datetime.now()
    
    post_queue = []
    for media in media_files:
        for page in target_pages:
            is_video = 'video' in media.file_type.lower()
            if is_video and not page.allow_videos:
                continue
            if not is_video and not page.allow_images:
                continue
            post_queue.append((page, media))
            
    if not post_queue:
        return jsonify({'message': 'No posts were scheduled. Check page content restrictions against selected media types.'}), 200


    for page, media in post_queue:
        next_slot = get_next_available_slot(page.id, page.time_slots, last_scheduled_time)
        
        if not next_slot:
            break

        title, description = generate_post_content(title_template, description_template)

        new_post = ScheduledPost(
            page_id=page.id,
            media_file_id=media.id,
            title=title,
            description=description,
            scheduled_time=int(next_slot.timestamp()),
            media_type='video' if 'video' in media.file_type.lower() else 'image',
            is_active=True # Posts are active by default
        )
        
        db.session.add(new_post)
        last_scheduled_time = next_slot
        scheduled_count += 1
        
    db.session.commit()
    return jsonify({'message': f'{scheduled_count} unique posts have been scheduled across {len(target_pages)} pages.'}), 201

@app.route('/api/schedule_now', methods=['POST'])
def api_schedule_now():
    data = request.get_json()
    media_ids = data.get('mediaIds', [])
    page_ids = data.get('pageIds', [])
    title_template = data.get('titleTemplate', '')
    description_template = data.get('descriptionTemplate', '')

    if not media_ids or not page_ids:
        return jsonify({'error': 'No media or pages selected for posting.'}), 400

    media_files = MediaFile.query.filter(MediaFile.id.in_(media_ids)).all()
    target_pages = Page.query.filter(Page.id.in_(page_ids)).all()

    if not media_files or not target_pages:
        return jsonify({'error': 'Selected media or pages not found.'}), 404

    success_count = 0
    failure_details = []

    for page in target_pages:
        for media in media_files:
            is_video = 'video' in media.file_type.lower()
            if is_video and not page.allow_videos:
                failure_details.append(f"Page {page.page_name}: Videos restricted.")
                continue
            if not is_video and not page.allow_images:
                failure_details.append(f"Page {page.page_name}: Images restricted.")
                continue

            # Create a temporary post entry for synchronous execution
            title, description = generate_post_content(title_template, description_template)
            
            temp_post = ScheduledPost(
                page_id=page.id,
                media_file_id=media.id,
                title=title,
                description=description,
                # Schedule in the immediate past so the worker can find it if needed, 
                # but we execute it synchronously now.
                scheduled_time=int(time.time()) - 5, 
                media_type='video' if is_video else 'image',
                status='processing',
                is_active=True
            )
            db.session.add(temp_post)
            db.session.commit()
            
            # Execute immediately (publish_now=True)
            success, result = post_to_facebook(temp_post.id, publish_now=True)
            
            if success:
                success_count += 1
            else:
                failure_details.append(f"Page {page.page_name}: Failed - {result}")

    db.session.commit() # Commit status changes from post_to_facebook
    
    return jsonify({
        'message': f'{success_count} posts published successfully. {len(failure_details)} failures.',
        'failures': failure_details
    }), 200

@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
def delete_folder(folder_id):
    folder = MediaFolder.query.get(folder_id)
    if not folder:
        return jsonify({'error': 'Folder not found'}), 404
        
    # Optional: Protect the Default Folder
    if folder.name == 'Default Folder':
        return jsonify({'error': 'The Default Folder cannot be deleted.'}), 400

    try:
        # 1. Find all files in this folder
        files = MediaFile.query.filter_by(folder_id=folder_id).all()
        
        for media in files:
            # 2. Delete physical file from disk
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            
            # 3. Remove any Scheduled Posts that use this media (to prevent errors)
            ScheduledPost.query.filter_by(media_file_id=media.id).delete()
            
            # 4. Delete the media record
            db.session.delete(media)

        # 5. Finally, delete the folder
        db.session.delete(folder)
        db.session.commit()
        return jsonify({'message': 'Folder and all contents deleted successfully.'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

def get_next_available_slot(page_id, time_slots_str, start_dt):
    """Finds the next unique time slot for scheduling."""
    time_slots = sorted([ts.strip() for ts in time_slots_str.split(',') if ts.strip()])
    if not time_slots: return None
    
    # Parse slots into hour/minute tuples for comparison
    parsed_slots = []
    for ts in time_slots:
        try:
            h, m = map(int, ts.split(':'))
            parsed_slots.append((h, m))
        except:
            continue
            
    if not parsed_slots: return None
    
    # Start searching
    current_dt = start_dt + timedelta(minutes=1)
    current_dt = current_dt.replace(second=0, microsecond=0)
    
    # Limit search to 60 days (handles 170+ posts easily)
    search_limit = current_dt + timedelta(days=60)
    
    while current_dt < search_limit:
        # Current time components
        c_h, c_m = current_dt.hour, current_dt.minute
        
        # Check if current time is a valid slot
        is_slot = False
        for h, m in parsed_slots:
            if c_h == h and c_m == m:
                is_slot = True
                break
        
        if is_slot:
            scheduled_unix = int(current_dt.timestamp())
            existing_post = ScheduledPost.query.filter(
                ScheduledPost.page_id == page_id,
                ScheduledPost.scheduled_time == scheduled_unix,
                ScheduledPost.status != 'failed'
            ).first()

            if not existing_post:
                return current_dt

        # --- OPTIMIZATION & FIX ---
        # If we are past the last slot of the day, jump to the first slot of TOMORROW.
        last_h, last_m = parsed_slots[-1]
        if (c_h > last_h) or (c_h == last_h and c_m >= last_m):
            # Jump to tomorrow first slot
            first_h, first_m = parsed_slots[0]
            # Use timedelta to safely add a day (Fixes "day out of range" error)
            current_dt = current_dt + timedelta(days=1)
            current_dt = current_dt.replace(hour=first_h, minute=first_m)
        else:
            # Otherwise just check next minute (or could jump to next slot, but minute safe)
            current_dt += timedelta(minutes=1)
            
    return None


@app.route('/api/schedule', methods=['GET'])
def api_schedule():
    posts = ScheduledPost.query.all()
    
    posts_data = []
    page_lookup = {p.id: p.page_name for p in Page.query.all()}
    media_lookup = {m.id: m.original_name for m in MediaFile.query.all()}

    for post in posts:
        posts_data.append({
            'id': post.id,
            'page_id': post.page_id,
            'page_name': page_lookup.get(post.page_id, 'Unknown Page'),
            'title': post.title,
            'description': post.description,
            'scheduled_time': post.scheduled_time,
            'media_type': post.media_type,
            'media_name': media_lookup.get(post.media_file_id, 'Missing Media'),
            'status': post.status,
            'is_active': post.is_active,
            'fb_post_id': post.fb_post_id,
            'error_message': post.error_message
        })
        
    return jsonify(posts_data)

# --- Worker Endpoint and Task Actions ---

@app.route('/api/worker/run', methods=['POST'])
def api_worker_run():
    """
    Cron job endpoint to process scheduled posts.
    Prevents double-posting by marking items as 'processing' immediately.
    """
    now_unix = int(time.time())
    
    # 1. Find posts that are ready
    posts_to_process = ScheduledPost.query.filter(
        ScheduledPost.status == 'scheduled',
        ScheduledPost.is_active == True,
        ScheduledPost.scheduled_time <= now_unix
    ).all()
    
    if not posts_to_process:
        # app.logger.info('Worker ran: No active posts due for execution.') # Optional: Silence logs
        return jsonify({'message': 'Worker ran: No active posts due for execution.'}), 200

    # --- CRITICAL FIX: Mark them as 'processing' IMMEDIATELY ---
    # This prevents the next worker tick (10s later) from grabbing the same posts
    # while the videos are still uploading.
    for post in posts_to_process:
        post.status = 'processing'
    db.session.commit()
    # -----------------------------------------------------------

    processed_count = 0
    failed_count = 0
    
    for post in posts_to_process:
        # We pass publish_now=True because the worker has already determined it's time.
        success, result = post_to_facebook(post.id, publish_now=True)
        if success:
            processed_count += 1
        else:
            failed_count += 1
            
    # Commit final results (status='posted' or 'failed' is set inside post_to_facebook)
    db.session.commit()
    
    message = f"Worker ran: {processed_count} posts sent, {failed_count} failures."
    app.logger.info(message)
    return jsonify({'message': message, 'success_count': processed_count, 'failed_count': failed_count}), 200

@app.route('/api/schedule/edit_time/<int:post_id>', methods=['POST'])
def api_schedule_edit_time(post_id):
    post = ScheduledPost.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found.'}), 404
        
    new_time = request.get_json().get('newTime')
    if not new_time or new_time <= int(time.time()):
        return jsonify({'error': 'Invalid or past scheduled time.'}), 400
        
    post.scheduled_time = new_time
    # Reset status if it was failed, as the user is actively fixing it.
    if post.status == 'failed':
        post.status = 'scheduled'
        
    db.session.commit()
    return jsonify({'message': 'Scheduled time updated.'}), 200

@app.route('/api/schedule/toggle_status/<int:post_id>', methods=['POST'])
def api_schedule_toggle_status(post_id):
    post = ScheduledPost.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found.'}), 404
        
    post.is_active = not post.is_active
    db.session.commit()
    return jsonify({'message': 'Status updated.', 'is_active': post.is_active}), 200

@app.route('/api/schedule/delete/<int:post_id>', methods=['DELETE'])
def api_schedule_delete(post_id):
    post = ScheduledPost.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found.'}), 404
        
    db.session.delete(post)
    db.session.commit()
    return jsonify({'message': 'Scheduled post deleted.'}), 200

@app.route('/api/schedule/retry/<int:post_id>', methods=['POST'])
def api_schedule_retry(post_id):
    post = ScheduledPost.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found.'}), 404
    
    if post.status != 'failed':
        return jsonify({'error': 'Post status must be "failed" to retry.'}), 400
        
    # Reset status and activation for the worker to pick it up immediately
    post.status = 'scheduled'
    post.is_active = True
    post.scheduled_time = int(time.time()) - 5 # Schedule it in the past for immediate worker pickup
    post.error_message = None

    db.session.commit()
    
    # Optionally trigger the worker immediately for instant feedback
    success, result = post_to_facebook(post.id)
    
    if success:
        return jsonify({'message': 'Retry successful. Post sent to Facebook.'}), 200
    else:
        return jsonify({'message': f'Retry initiated. Execution failed again: {result}. Check logs.'}), 200

if __name__ == '__main__':
    # Add handler for detailed logging to the console (important for debugging AWS)
    import logging
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host='0.0.0.0')