import os
from dotenv import load_dotenv
load_dotenv()

GMAIL_PUBSUB_TOPIC = os.getenv("GMAIL_PUBSUB_TOPIC")
token_json_path = "Inputs/token.json"
credentials_json_path = "Inputs/credentials.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

LOCAL_SQLITE = os.path.join(BASE_DIR, "instance", "emails.db")
db_path = f"sqlite:///{LOCAL_SQLITE}"
DEDUPLICATION_INDEX_PATH = os.path.join(BASE_DIR, "instance", "faiss_hnsw.index")
backend_log_path = "Backend_logging.txt"
frontend_log_path = "Frontend_logging.txt"
CHROME_DRIVER_PATH = "Inputs/chromedriver-win64/chromedriver.exe"

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
openai_model_name = "gpt-4o-mini"
huggingface_model_name = "meta-llama/Llama-3.1-8B-Instruct"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
JINA_API_KEY = os.getenv("JINA_API_KEY")
USE_OPENAI = True
SAVE_EMAILS_LOCAL = True

TARGET_COMPANIES = {
    'Nomura Research Institute',
    'NRI',
    '"Nomura research Institute" "NRI"',
    'Nomura Research Institute (NRI)',
    'Mitsubishi Research Institute',
    'MRI',
    '"Mitsubishi Research Institute" "MRI"',
    'Mitsubishi Research Institute (MRI)',
    'McKinsey',
    'McKinsey & Co.',
    'McKinsey & Company',
    'EY (Ernst & Young)',
    '"Boston Consulting Group" "BCG"',
    'BCG',
    'Boston Consulting Group',
    'Boston Consulting Group (BCG)',
    'Bain & Co.',
    'Bain & Company',
    'Accenture',
    '"Deloitte"',
    'Deloitte',
    '"Ernst & Young" "EY"',
    'Ernst & Young',
    'EY',
    'PwC',
    'KPMG',
    '"PricewaterhouseCoopers"',
    '"PricewaterhouseCoopers" "PwC"',
    'PricewaterhouseCoopers (PwC)'
}

use_jina_ai = True
encoding_name = "cl100k_base"
max_input_token: int = 128000