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
from Backend.database import app, db, Email

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app.logger.setLevel(logging.INFO)
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

app.secret_key = 'nri_gkc_cia'

# System-wide login credentials
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

def run_script_task(name, script_path):
    """
    Runs a specific python script with robust locking to prevent overlaps.
    """
    # Create a unique lock file for each task name
    safe_name = name.lower().replace(" ", "_")
    lock_file = os.path.join(config.BASE_DIR, "instance", f"{safe_name}.lock")
    
    # Robust Locking: Check for stale PID
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                content = f.read().strip()
                old_pid = int(content) if content else 0
            if old_pid > 0 and is_process_running(old_pid):
                app.logger.info(f"Scheduler [{name}]: Already running (PID: {old_pid}). Skipping.")
                return
            else:
                app.logger.warning(f"Scheduler [{name}]: Cleaning up stale lock (PID {old_pid}).")
                os.remove(lock_file)
        except Exception as e:
            app.logger.error(f"Scheduler [{name}]: Error checking lock: {e}")
            if os.path.exists(lock_file): os.remove(lock_file)

    # Write current PID to lock file
    try:
        with open(lock_file, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        app.logger.error(f"Scheduler [{name}]: Failed to create lock file: {e}")
        return

    try:
        app.logger.info(f"Scheduler [{name}]: Starting (PID: {os.getpid()})")
        
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=config.BASE_DIR,
            text=True,
            bufsize=1
        )
        
        for line in process.stdout:
            print(f"[{name}] {line.strip()}", flush=True)
        
        process.wait()
        app.logger.info(f"Scheduler [{name}]: Completed with exit code {process.returncode}")

    except Exception as e:
        app.logger.error(f"Scheduler [{name}] Critical Error: {e}")
    finally:
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except: pass

def run_email_extraction():
    run_script_task('Email Extraction', os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction.py'))

def run_url_extraction():
    run_script_task('URL Extraction', os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction_url.py'))

# --- Scheduler Initialization ---
# We use a global variable to ensure we don't start multiple schedulers in the same process
_scheduler_started = False

def start_scheduler():
    global _scheduler_started
    if _scheduler_started: return
    
    # In Gunicorn, we only want the scheduler in the Master process (if using --preload)
    # or we handle it via a dedicated worker. For this project, we'll use a lock-check approach.
    
    scheduler = BackgroundScheduler()
    
    # 1. Email Extraction: Every 2 hours
    scheduler.add_job(func=run_email_extraction, trigger="interval", hours=2)
    
    # 2. URL Extraction: Once daily at 8:00 AM
    scheduler.add_job(func=run_url_extraction, trigger="cron", hour=8, minute=0)
    
    # Initial run for Email Extraction 30s after boot for immediate feedback
    startup_delay = datetime.now() + timedelta(seconds=30)
    scheduler.add_job(func=run_email_extraction, trigger="date", run_date=startup_delay)
    
    scheduler.start()
    _scheduler_started = True
    app.logger.info(f"Background Scheduler started. Email: 2h interval, URL: Daily 08:00 AM.")

# Start scheduler if not in reloader child and not in debug mode (or forced)
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug or os.environ.get('START_SCHEDULER') == 'true':
    start_scheduler()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
