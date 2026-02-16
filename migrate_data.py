
import sys
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add project root to path to import app
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import app, db, Email
import config

def migrate(render_db_url):
    print("\n" + "="*50)
    print("🚀 STARTED: Migrating Local SQLite Data to Render Postgres")
    print("="*50 + "\n")

    # 1. READ LOCAL DATA
    # Force app to assume local settings to read SQLite
    print("📂 Step 1: Reading data from Local SQLite (instance/emails.db)...")
    
    # We create a specific app context or manually connect to SQLite to be safe
    # But since app.py defaults to local config if env vars aren't set, 
    # and we act as if we are local, it should preserve the SQLite.
    # However, if the user set ENV vars locally, this might break.
    # Let's explicitly bind the local engine.
    
    local_db_path = f"sqlite:///{os.path.join(config.BASE_DIR, 'instance', 'emails.db')}"
    local_engine = create_engine(local_db_path)
    LocalSession = sessionmaker(bind=local_engine)
    local_session = LocalSession()
    
    try:
        # We need to map the 'Email' class to this session.
        # But 'Email' is bound to 'db' (Flask-SQLAlchemy). 
        # This is tricky without Flask context.
        # Easier approach: Use pandas to read raw SQL.
        import pandas as pd
        
        # Read all data into DataFrame
        df = pd.read_sql("SELECT * FROM email", local_engine)
        print(f"✅ Found {len(df)} emails in local database.")
        
    except Exception as e:
        print(f"❌ Error reading local data: {e}")
        return

    if df.empty:
        print("⚠️ No data to migrate.")
        return

    # 2. WRITE TO REMOTE
    print(f"\n🌍 Step 2: Connecting to Render Postgres...")
    
    if not render_db_url.startswith("postgres"):
        print("❌ Invalid URL. It must start with 'postgres://' or 'postgresql://'")
        return
        
    # Fix for SQLAlchemy 1.4+ (postgres:// -> postgresql://)
    if render_db_url.startswith("postgres://"):
        render_db_url = render_db_url.replace("postgres://", "postgresql://", 1)

    remote_engine = create_engine(render_db_url)
    
    # Ensure table exists on remote
    # We can trust 'db.create_all()' ran on Render, OR we can use to_sql to create it.
    # Using 'if_exists="append"' is best.
    
    print("📤 Step 3: Uploading data to Render (this might take a minute)...")
    try:
        # Write to 'email' table
        # chunksize helps with network stability
        df.to_sql('email', remote_engine, if_exists='append', index=False, chunksize=100)
        print(f"✅ SUCCESS! {len(df)} emails migrated to Render Postgres.")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        print("\nPossible reasons:")
        print("1. Connection URL is wrong.")
        print("2. Required libraries missing (psycopg2-binary).")
        print("3. Table schema mismatch (if Render DB isn't empty).")

if __name__ == "__main__":
    print("Welcome to the GKC Data Migration Tool!")
    print("This script moves your local 'emails.db' history to the cloud.")
    
    url = input("\nPaste your Render Internal Database URL: ").strip()
    
    if url:
        migrate(url)
    else:
        print("Operation cancelled.")
