import logging
import os
import sys
import math
import subprocess
from datetime import datetime
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

if config.SAVE_EMAILS_LOCAL:
    app.config['SQLALCHEMY_DATABASE_URI'] = config.db_path
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = config.DATABASE_URI

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
    
    if config.SAVE_EMAILS_LOCAL:
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
    else:
        # For non-local, assume SQL handling (though date format suggests this might be tricky in SQL too without conversion)
        # Keeping existing logic for else block but might need update if used. Assuming SAVE_EMAILS_LOCAL is primary.
        # If we need to support SQL date filtering here:
        if start_date_str:
             query = query.filter(func.to_date(Email.date, "DD-MM-YY") >= start_date_str)
        if end_date_str:
             query = query.filter(func.to_date(Email.date, "DD-MM-YY") <= end_date_str)
             
        pagination = query.order_by(func.to_date(Email.date, "DD-MM-YY").desc()).paginate(page=page, per_page=per_page, error_out=False)
        items, total, pages = pagination.items, pagination.total, pagination.pages
        
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
def is_process_running(pid):
    try:
        # Check if process exists. signal 0 does nothing but error if process missing
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def run_data_extraction():
    try:
        # Lock check - Move to instance folder to avoid reloader loop
        lock_file = os.path.join(config.BASE_DIR, "instance", "data_extraction.pid")
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r') as f:
                    old_pid = int(f.read().strip())
                if is_process_running(old_pid):
                    app.logger.info(f"Scheduler: Email Extraction already running (PID: {old_pid}). Skipping.")
                    return
            except Exception:
                pass # Ignore corrupt lock file

        app.logger.info("Scheduler: Initiating Email Extraction.")
        backend_script = os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction.py')
        log_file_path = os.path.join(config.BASE_DIR, "instance", "scheduler_run_log.txt")
        
        with open(log_file_path, "a") as f:
            f.write(f"\n[{datetime.now()}] --- Email Extraction START ---\n")
            f.flush()
        
        # Use shell=True and a command string for better reliability on Windows
        cmd = f'"{sys.executable}" "{backend_script}"'
        f_out = open(log_file_path, "a")
        proc = subprocess.Popen(cmd, shell=True, stdout=f_out, stderr=subprocess.STDOUT, text=True, cwd=config.BASE_DIR)
        
        # Write new PID
        with open(lock_file, 'w') as f:
            f.write(str(proc.pid))
            
        app.logger.info(f"Scheduler: Email Extraction process spawned with cmd: {cmd} (PID: {proc.pid})")
    except Exception as e: app.logger.error(f"Email Automation Error: {e}")

def run_url_extraction():
    try:
        # Lock check - Move to instance folder
        lock_file = os.path.join(config.BASE_DIR, "instance", "url_extraction.pid")
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r') as f:
                    old_pid = int(f.read().strip())
                if is_process_running(old_pid):
                    app.logger.info(f"Scheduler: URL Extraction already running (PID: {old_pid}). Skipping.")
                    return
            except Exception:
                pass

        app.logger.info("Scheduler: Initiating URL Extraction.")
        backend_script = os.path.join(config.BASE_DIR, 'Backend', 'Data_Extraction_url.py')
        log_file_path = os.path.join(config.BASE_DIR, "instance", "scheduler_run_log.txt")
        
        with open(log_file_path, "a") as f:
            f.write(f"\n[{datetime.now()}] --- URL Extraction START ---\n")
            f.flush()
            
        cmd = f'"{sys.executable}" "{backend_script}"'
        f_out = open(log_file_path, "a")
        proc = subprocess.Popen(cmd, shell=True, stdout=f_out, stderr=subprocess.STDOUT, text=True, cwd=config.BASE_DIR)
        
        with open(lock_file, 'w') as f:
            f.write(str(proc.pid))

        app.logger.info(f"Scheduler: URL Extraction process spawned with cmd: {cmd} (PID: {proc.pid})")
    except Exception as e: app.logger.error(f"URL Automation Error: {e}")

# Standard Flask pattern: run once in child process or if debug is off
# Standard Flask pattern: run once in child process or if debug is off
# Or if reloader is disabled, run in the main process
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug or os.environ.get('FLASK_RUN_FROM_CLI') == 'true' or True: # Force start for now as we are in a single process dev mode

    scheduler = BackgroundScheduler()
    scheduler.add_job(func=run_data_extraction, trigger="interval", hours=2)
    scheduler.add_job(func=run_url_extraction, trigger="interval", hours=2)
    # Staggered immediate runs to avoid file lock conflict
    scheduler.add_job(func=run_data_extraction, trigger="date", run_date=datetime.now())
    # Start URL extraction 30 seconds later to let the first one breathe
    from datetime import timedelta
    scheduler.add_job(func=run_url_extraction, trigger="date", run_date=datetime.now() + timedelta(seconds=30))
    scheduler.start()
    app.logger.info("GINAI GKC Background Scheduler started.")

if __name__ == '__main__':
    print("\n" + "="*50)
    print("--> GKC APP STARTED: Sorting Fix Applied <--")
    print("="*50 + "\n")
    app.run(debug=True, use_reloader=False)
