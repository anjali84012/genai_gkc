import logging
import os
import sys
import math
import subprocess
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

import pandas as pd
from flask import Flask, render_template, request, redirect, flash, session, url_for, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func
from apscheduler.schedulers.background import BackgroundScheduler

import config

app = Flask(__name__)
app.secret_key = 'nri_gkc_cia'

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app.logger.setLevel(logging.INFO)
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

app.config['SQLALCHEMY_DATABASE_URI'] = config.db_path

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


class Email(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(255))
    subject = db.Column(db.String(255))
    date = db.Column(db.String(50))
    time = db.Column(db.String(50))
    link = db.Column(db.String(5000))
    title = db.Column(db.Text)
    body = db.Column(db.Text)
    response_generated = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(255))
    category_code = db.Column(db.String(50))
    summary = db.Column(db.Text)
    summary_japanese = db.Column(db.Text)
    decision = db.Column(db.String(50))
    reasoning = db.Column(db.Text)
    satake_score = db.Column(db.Integer, default=0)
    kajikawa_score = db.Column(db.Integer, default=0)
    satake_done_scoring = db.Column(db.Boolean, default=False)
    kajikawa_done_scoring = db.Column(db.Boolean, default=False)
    total_score = db.Column(db.Integer, default=0)
    final_flag = db.Column(db.Boolean, default=False)
    remove_flag = db.Column(db.Boolean, default=False)
    disapproved = db.Column(db.Boolean, default=False)
    duplicated_or_not = db.Column(db.Boolean, default=False)
    reason_of_duplication = db.Column(db.Text)

# Ensure tables exist (safe: checks first, doesn't overwrite)
with app.app_context():
    db.create_all()

USERS = {"auditor@nriindia.co.in": {"password": "gkc123", "role": "auditor"}}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'): return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def _parse_date(date_str):
    try: return datetime.strptime(date_str, "%d-%m-%y")
    except: return datetime.min

@app.route('/')
def index(): return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, password = request.form['email'].strip(), request.form['password'].strip()
        user = USERS.get(email)
        if user and user['password'] == password:
            session['logged_in'], session['user_role'] = True, user['role']
            session['user_name'] = 'Auditor'
            return redirect(url_for('intro'))
        flash("Invalid credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/intro')
@login_required
def intro(): return render_template('intro.html', target_url=url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    page, per_page = request.args.get('page', 1, type=int), 20
    company, category = request.args.get('company', '').strip(), request.args.get('category', '').strip()
    query = Email.query
    start_date_str = request.args.get('start_date', '').strip()
    end_date_str = request.args.get('end_date', '').strip()

    if company: query = query.filter(Email.company_name.ilike(f"%{company}%"))
    if category: query = query.filter(Email.category.ilike(f"%{category}%"))
    
    # ALWAYS use Python-side filtering for consistency across DBs (SQLite/Postgres)
    # and to handle custom date format "DD-MM-YY" reliably.
    all_emails = query.all()
    
    # Filter by date range (Python side because date is stored as DD-MM-YY string)
    if start_date_str:
        try:
            s_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            all_emails = [e for e in all_emails if _parse_date(e.date) >= s_date]
        except ValueError: pass
        
    if end_date_str:
        try:
            e_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            # Add time component to end date to include the whole day
            e_date = e_date.replace(hour=23, minute=59, second=59)
            all_emails = [e for e in all_emails if _parse_date(e.date) <= e_date]
        except ValueError: pass

    # Sort by date DESC, then ID DESC to ensure newest items for same date appear first
    sorted_emails = sorted(all_emails, key=lambda x: (_parse_date(x.date), x.id), reverse=True)
    total = len(sorted_emails)
    items = sorted_emails[(page-1)*per_page : page*per_page]
    pages = math.ceil(total / per_page)
        
    return render_template('dashboard.html', emails=items, page=page, pages=pages, total=total, 
                           company_filter=company, category_filter=category, 
                           start_date=start_date_str, end_date=end_date_str)

@app.route('/download_data')
@login_required
def download_data():
    company = request.args.get('company', '').strip()
    category = request.args.get('category', '').strip()
    start_date_str = request.args.get('start_date', '').strip()
    end_date_str = request.args.get('end_date', '').strip()

    query = Email.query
    if company: query = query.filter(Email.company_name.ilike(f"%{company}%"))
    if category: query = query.filter(Email.category.ilike(f"%{category}%"))
    
    emails = query.all()
    
    # Apply Python-side date filtering (consistent with dashboard)
    if start_date_str:
        try:
            s_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            emails = [e for e in emails if _parse_date(e.date) >= s_date]
        except ValueError: pass
        
    if end_date_str:
        try:
            e_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            e_date = e_date.replace(hour=23, minute=59, second=59)
            emails = [e for e in emails if _parse_date(e.date) <= e_date]
        except ValueError: pass
    rows = [{'ID': f"{e.date}_{e.id}", 'Company Name': e.company_name, 'Subject': e.subject, 'Date': e.date, 'Time': e.time, 'Link': e.link, 'Title': e.title, 'Body': e.body, 'Category': e.category, 'Category Code': e.category_code, 'Summary': e.summary, 'Summary Japanese': e.summary_japanese, 'Decision': e.decision, 'Reasoning': e.reasoning, 'Disapproved': e.disapproved, 'Duplicate': e.duplicated_or_not, 'Reason of duplication': e.reason_of_duplication} for e in emails]
    if not rows: flash("No data"); return redirect(url_for('dashboard'))
    df = pd.DataFrame(rows)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Auditor Data')
    output.seek(0)
    return send_file(output, download_name=f"auditor_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx", as_attachment=True)

@app.route('/gmail_webhook', methods=['POST'])
def gmail_webhook():
    # Placeholder for potential push notifications
    return jsonify({"status": "received"}), 200

def is_process_running(pid):
    if pid <= 0: return False
    if os.name == 'nt':
        import subprocess
        try:
            output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.STDOUT, text=True)
            return str(pid) in output
        except: return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

def run_sequential_extraction():
    """
    Runs data extraction and URL extraction sequentially with robust locking.
    """
    lock_file = os.path.join(config.BASE_DIR, "instance", "sequential_extraction.lock")
    
    # Robust Locking: Check for stale PID
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            if is_process_running(old_pid):
                app.logger.info(f"Scheduler: Task already running (PID: {old_pid}). Skipping.")
                return
            else:
                app.logger.warning(f"Scheduler: Found stale lock file (PID {old_pid} not running). Cleaning up.")
                os.remove(lock_file)
        except Exception as e:
            app.logger.error(f"Scheduler: Error reading lock file: {e}")
            if os.path.exists(lock_file): os.remove(lock_file)

    # Write current PID to lock file
    try:
        with open(lock_file, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        app.logger.error(f"Scheduler: Failed to create lock file: {e}")
        return

    try:
        app.logger.info(f"Scheduler: Starting Task (PID: {os.getpid()})")
        
        scripts = [
            ('Email Extraction', os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction.py')),
            ('URL Extraction', os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction_url.py'))
        ]

        for name, script in scripts:
            app.logger.info(f"Scheduler: --> Running {name}...")
            process = subprocess.Popen(
                [sys.executable, script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=config.BASE_DIR,
                text=True,
                bufsize=1
            )
            
            for line in process.stdout:
                # Log to stdout so it shows up in Railway/Render logs
                print(f"[{name}] {line.strip()}", flush=True)
            
            process.wait()
            app.logger.info(f"Scheduler: --> {name} Completed with exit code {process.returncode}")

        app.logger.info("Scheduler: All tasks finished successfully.")

    except Exception as e:
        app.logger.error(f"Scheduler Critical Error: {e}")
    finally:
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except: pass

# --- Scheduler Initialization ---
# We use a global variable to ensure we don't start multiple schedulers in the same process
_scheduler_started = False

def start_scheduler():
    global _scheduler_started
    if _scheduler_started: return
    
    # In Gunicorn, we only want the scheduler in the Master process (if using --preload)
    # or we handle it via a dedicated worker. For this project, we'll use a lock-check approach.
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_sequential_extraction, trigger="interval", hours=2)
    
    # Initial run after a short delay (30s instead of 2m for faster feedback)
    startup_delay = datetime.now() + timedelta(seconds=30)
    scheduler.add_job(func=run_sequential_extraction, trigger="date", run_date=startup_delay)
    
    scheduler.start()
    _scheduler_started = True
    app.logger.info(f"Background Scheduler started. First run at {startup_delay.strftime('%H:%M:%S')}")

# Start scheduler if not in reloader child and not in debug mode (or forced)
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug or os.environ.get('START_SCHEDULER') == 'true':
    start_scheduler()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
