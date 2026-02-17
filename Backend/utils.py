from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from newspaper import Article
from mtranslate import translate
try:
    import prompts
except ImportError:
    from Backend import prompts
from bs4 import BeautifulSoup
import re
import json
import requests
from openai import OpenAI
from huggingface_hub import InferenceClient
import pandas as pd
import logging
from datetime import datetime
import tiktoken
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

#------------------------------------------------------------------------------------------------------
                                    ## Logging and Database Initialization ##
#------------------------------------------------------------------------------------------------------

def configure_logging(module_type='backend', log_filename='application.log', base_dir='Logs'):
    """
    Configures logging under Logs/<module_type>/<today's date>/<log_filename>.

    Args:
        module_type (str): Either 'backend' or 'frontend' to create logs under respective folders.
        log_filename (str): Name of the log file to create.
        base_dir (str): Root directory for logs.
    """
    today_str = datetime.today().strftime('%Y-%m-%d')
    log_dir = os.path.join(base_dir, module_type, today_str)
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, log_filename)

    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S', 
        level=logging.INFO,
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    # Example: if you need specific handlers or formatting, add here
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.propagate = False  # Prevent duplication if needed


def flask_sql_alchemy_db():

    """
    Initializes Flask app and SQLAlchemy database.

    Returns:
        db (SQLAlchemy instance), app (Flask app instance)
    """

    app = Flask(__name__)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = config.db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db = SQLAlchemy(app)
    
    with app.app_context():
        # Only enable WAL mode for SQLite
        if "sqlite" in config.db_path:
            try:
                db.session.execute(text("PRAGMA journal_mode=WAL;"))
                db.session.commit()
                print("INFO: Database set to WAL mode.")
            except Exception as e:
                print(f"WARNING: Could not set WAL mode: {e}")

    return db, app

#------------------------------------------------------------------------------------------------------
                                    ## Article Content Extraction ##
#------------------------------------------------------------------------------------------------------
def fetch_article_content(url, html=None):

    """
    Fetches article content using Newspaper3k.

    Args:
        url (str): URL of the article.
        html (str, optional): Pre-fetched HTML content.

    Returns:
        str: HTML content or None.
    """

    try:
        article = Article(url, language="en")  # use "ja" for Japanese if needed
        if html:
            article.set_html(html)
        else:
            article.download()
            
        article.parse()
        return article.html
    except Exception:
        return None
        
def is_js_rendered(html):

    """
    Checks if HTML is JavaScript-rendered.

    Args:
        html (str): HTML content.

    Returns:
        bool: True if JS-rendered, False otherwise.
    """

    return (
        'id="__next"' in html or 
        'id="root"' in html or 
        'type="application/json"' in html
    )
    
def fetch_using_selenium(url):
    """
    Fetch webpage HTML using Selenium as a fallback for JS-rendered sites.
    Returns full page_source or None.
    """

    try:
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        # chrome_options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe" # Commented out to auto-detect
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--ignore-certificate-errors")

        # Ensure absolute path for driver
        driver_path = os.path.join(config.BASE_DIR, config.CHROME_DRIVER_PATH)
        if not os.path.exists(driver_path):
             # Fallback to creating path if config path is relative to CWD not BASE_DIR
             driver_path = os.path.abspath(config.CHROME_DRIVER_PATH)

        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        

        driver.get(url)

        # Wait for page load
        driver.implicitly_wait(10)

        html = driver.page_source
        driver.quit()
        return html

    except Exception as e:
        print(f"[ERROR] Selenium failed for {url}: {e}")
        try:
            driver.quit()
        except:
            pass
        return None


def fetch_using_jina(url):

    """
    Fetches content using Jina AI.

    Args:
        url (str): URL of the webpage.

    Returns:
        str: HTML content or None.
    """

    headers = {
        "Authorization": f"Bearer {config.JINA_API_KEY}",
    }
    final_url = f"https://r.jina.ai/{url}"
    try:
        response = requests.get(final_url, headers=headers)
        if '"data":null' in response.text:
            return None
        return response.text  # or parse/clean as needed
    except Exception as e:
        print(f"[ERROR] Jina failed for {url}: {e}")
        return None

def get_article_html(url):
    NON_ARTICLE_KEYWORDS = ["nhl.com", "school", "award", "sports"]

    if any(k in url.lower() for k in NON_ARTICLE_KEYWORDS):
        logging.warning(f"Skipping non-article URL: {url}")
        return None
    
    """
    Fetches article HTML using both Newspaper and Jina with fallback methods
    using the following steps: 
    
    Step 1) It wil check if the url is not JS rendered.
    Step 2) If False, then it will use fetch_article_content (Using Newspaper) and return the content
    Step 3) If True, then it will use fetch_using_jina (Using Jina) and return the content
    Step 4) If both are False, then it will return None

    Args:
        url (str): URL of the webpage.

    Returns:
        str: HTML content or None.
    """
    
    """
    Fetches article HTML using Newspaper → Jina → Selenium sequence.
    """

    try:
        # 1️⃣ If using Jina directly
        if config.use_jina_ai:
            try:
                html = fetch_using_jina(url)
                if html and len(html) > 500: # Check for meaningful content
                    return html
            except Exception as e:
                print(f"Jina failed: {e}")

        # 2️⃣ Try normal GET first
        html = ""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            response = requests.get(url, headers=headers, timeout=40)
            response.encoding = response.apparent_encoding
            html = response.text
        except:
            html = ""

        # 3️⃣ If NOT JS-rendered → try Newspaper
        if html and len(html) > 500 and not is_js_rendered(html):
            article_html = fetch_article_content(url, html=html)
            if article_html and len(article_html) > 200:
                return article_html

        # 4️⃣ Try Jina fallback
        if config.use_jina_ai: # Retry Jina
             jina_html = fetch_using_jina(url)
             if jina_html:
                return jina_html

        # 5️⃣ Try Selenium as LAST fallback
        # Also try if html was short/empty
        selenium_html = fetch_using_selenium(url)
        if selenium_html:
            return selenium_html

        return None

    except Exception as e:
        print(f"[ERROR] While scraping {url}: {e}")
        return None

    
#------------------------------------------------------------------------------------------------------
                                    ## Data Cleaning Functions ##
#------------------------------------------------------------------------------------------------------

def html_to_links(html):
    """
    Converts HTML to text while preserving links in Markdown format [text](href).
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
        
    # Convert links to markdown-like format
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        if text:
            a.replace_with(f" [{text}]({a['href']}) ")
            
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()

def html_to_clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()

def extract_json_from_output(text):
    """
    Extracts JSON content from LLM-generated text output.

    Args:
        text (str): Raw text output from LLM.

    Returns:
        dict or list: Parsed JSON data if extraction is successful, otherwise None.
    """
    try:
        # If it's already a JSON array or object
        parsed = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        pass  # Try regex if direct parsing fails

    # Try to extract from triple quotes using regex
    match = re.search(r"```?json\s*'''(.*?)'''", text, re.DOTALL)
    if not match:
        match = re.search(r"'''(.*?)'''", text, re.DOTALL)
    if not match:
        match = re.search(r"```json(.*?)```", text, re.DOTALL)
    if not match:
        match = re.search(r"\[(\s*{.*?})\s*\]", text, re.DOTALL)

    if match:
        try:
            extracted = match.group(1)
            # Wrap in brackets if only extracting inner list items
            if not extracted.strip().startswith("["):
                extracted = f"[{extracted}]"
            return json.loads(extracted)
        except json.JSONDecodeError as e:
            print("[ERROR] JSON parsing failed after regex match:", e)
            return None

    print("[WARNING] No valid JSON block found in Gemini response.")
    return None

def category_code_mapper(response):
    """
    Maps a given category name to its corresponding predefined category code.

    Args:
        response (str): Category name.

    Returns:
        str: Corresponding category code or error message.
    """
    try:
        # Define the category code map
        category_code_map = {key: f"C{str(i+1).zfill(2)}" for i, key in enumerate(prompts.category_dict.keys())}
        
        # Attempt to get the corresponding code for the response
        if response not in category_code_map:
            raise ValueError(f"Invalid category response: {response}")

        return category_code_map[response]
    except KeyError as e:
        print(f"KeyError: {e}")
        return "Invalid Category Code"
    except ValueError as e:
        print(f"ValueError: {e}")
        return "Invalid Category Code"
    except Exception as e:
        # Handle any other unexpected exceptions
        print(f"Unexpected error occurred: {e}")
        return "Error"
    
def summary_japanese_translate(response):
    """
    Translates the provided summary text from English to Japanese.

    Args:
        response (str): English text summary.

    Returns:
        str: Japanese translation or error message.
    """
    if not response:
        return ""
    try:
        return translate(response, to_language="ja", from_language="en")
    except Exception as e:
        print(f"Error translating to Japanese: {e}")
        return response  # Fallback to English summary on error
    
from langdetect import detect

def ensure_english(text):
    """
    Ensures text is in English by detecting language and translating if necessary.
    """
    if not text:
        return ""
    try:
        try:
            lang = detect(text)
        except:
            return text  # detection failed, return original

        if lang != 'en':
            translated = translate(text, "en", "auto")
            return translated
    except Exception as e:
        print(f"Translation to English failed: {e}")
    return text

#------------------------------------------------------------------------------------------------------
                                          ## LLM Functions ##
#------------------------------------------------------------------------------------------------------

def load_llm():
    """
    Initializes and returns an LLM client based on configuration.

    Returns:
        object: OpenAI or HuggingFace LLM client instance.
    """
    if config.USE_OPENAI:
        llm = OpenAI(
            api_key=config.OPENAI_API_KEY
        )
        return llm
    else:
        client = InferenceClient(
            api_key=config.HUGGINGFACE_API_KEY
            )
        return client
    
def title_text_generation_from_url(url, llm):
    """
    Generates title and text from URL using LLM.

    Args:
        url (str): URL to scrape.
        llm (object): LLM client instance.

    Returns:
        dict: Dictionary with keys 'URL', 'Title', 'Text'.
    """
    articles_data = []
    try:
        # STEP 1: Fetch article HTML
        html_content = get_article_html(url)
        if not html_content or not html_content.strip():
            raise ValueError("Empty or invalid HTML content")

        clean_text = html_to_clean_text(html_content)

        if not is_within_gpt41_token_limit(clean_text):
            clean_text = truncate_to_tokens(
                clean_text,
                max_tokens=config.max_input_token
            )

        # STEP 2: Create system prompt with HTML
        system_prompt = prompts.prompt_for_html_content

        messages = [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user", 
                    "content": f"Content:\n{clean_text}"
                }
            ]

        # STEP 3: LLM call based on config
        if config.USE_OPENAI:
            try:
                # For OpenAI, llm is ChatOpenAI
                response = llm.chat.completions.create(
                model = config.openai_model_name, 
                messages = messages
                )
                response = response.choices[0].message.content
                # print("Response:",response)
                cleaned_output = extract_json_from_output(response)
                
                if not cleaned_output or not isinstance(cleaned_output, list) or len(cleaned_output) == 0:
                     print(f"Warning: Empty or invalid JSON from LLM: {response[:100]}")
                     raise ValueError("LLM returned empty list")

                articles_data.append({
                    "url": url,
                    "title": cleaned_output[0].get(next((k for k in cleaned_output[0] if k.lower() == 'title'), 'title'), "No Title Found"),
                    "text": cleaned_output[0].get(next((k for k in cleaned_output[0] if k.lower() == 'text'), 'text'), "No content Found")
                })
            except Exception as e:
                print(f"Error generating additional response (OpenAI): {e}")
                articles_data.append({
                    "url": url,
                    "title": "No Title Found",
                    "text": "No content Found"
                })
                # Check clean text size for debugging
                print(f"DEBUG: Clean text size for failed URL {url}: {len(clean_text)}")
        else:
            try:
                response = llm.chat_completion(
                    model=config.huggingface_model_name, 
                    messages=messages
                    )
                response = response.choices[0].message.content
                
                cleaned_output = extract_json_from_output(response)
                if (
                    not cleaned_output or
                    not isinstance(cleaned_output, list) or
                    len(cleaned_output) == 0 or
                    "title" not in cleaned_output[0] or
                    "text" not in cleaned_output[0]
                ):
                    raise ValueError("LLM response missing title or text")

                articles_data.append({
                    "url": url,
                    "title": cleaned_output[0].get(next((k for k in cleaned_output[0] if k.lower() == 'title'), 'title'), "No Title Found"),
                    "text": cleaned_output[0].get(next((k for k in cleaned_output[0] if k.lower() == 'text'), 'text'), "No content Found")
                })
            except Exception as e:
                print(f"Error generating additional response (OpenAI): {e}")
                return {
                    "url": url,
                    "title": "No Title Found",
                    "text": "No content Found"
                }
    except Exception as e:
        print(f"[ERROR] LLM scraping failed for {url}: {e}")
        articles_data.append({
            "url": url,
            "title": "No Title Found",
            "text": "No content Found"
        })
    df = pd.DataFrame(articles_data)
    return {
        "title": df.iloc[0]["title"],
        "text": df.iloc[0]["text"]
        }

    
def generate_additional_response(article_text, llm):
    """
    Generates a structured response including category, summary, reasoning, 
    and decision from provided article text using LLM.

    Args:
        article_text (str): The text content of the article.
        llm (object): Instance of the LLM client (OpenAI or Hugging Face).

    Returns:
        dict: Parsed JSON response with keys Category, Summary, Reasoning, Decision or error details.
    """
    prompt = prompts.category_consulting_summary_prompt
    messages = [
            {
                "role": "system",
                "content": prompt
            },
            {
                "role": "user", 
                "content": f"Article Content:\n{article_text}"
            }
            ]
    if config.USE_OPENAI:
        try:
            response = llm.chat.completions.create(
                model=config.openai_model_name, 
                messages=messages
                )
            response = response.choices[0].message.content
            cleaned_output = extract_json_from_output(response)
            return cleaned_output
        except Exception as e:
            print(f"Error generating additional response (OpenAI): {e}")
            return {"Category": "Error", "Summary": "Error"," Reasoning": "Error", "Decision": "Error"}
    else:
        try: 
            response = llm.chat_completion(
                model=config.huggingface_model_name, 
                messages=messages
                )
            response = response.choices[0].message.content
            cleaned_output = extract_json_from_output(response)
            return cleaned_output
        except Exception as e:
            print(f"Error generating additional response (Hugging Face): {e}")
            return {"Category": "Error", "Summary": "Error"," Reasoning": "Error", "Decision": "Error"}

def initial_call_to_LLM(html,llm):
    """
    Performs the initial call to LLM to extract structured JSON from HTML content.

    Args:
        html (str): HTML content of the webpage.
        llm (object): Instance of the LLM client.

    Returns:
        dict: Extracted JSON response containing news titles and links.
    """
    clean_text = html_to_links(html)
    if not is_within_gpt41_token_limit(clean_text):
        clean_text = truncate_to_tokens(
            clean_text,
            max_tokens=config.max_input_token
        )
        
    messages = [
        {
            "role": "system",
            "content": prompts.prompt_initial_call()
        },
        {
            "role": "user", 
            "content": f"HTML content:\n{clean_text}"
        }
    ]

    if config.USE_OPENAI:
        response = llm.chat.completions.create(
            model=config.openai_model_name,
            messages=messages
        )

        response = response.choices[0].message.content
        extracted_json_response = extract_json_from_output(response)
        
        # Validation: Filter out invalid links (e.g. titles)
        valid_items = []
        if extracted_json_response and isinstance(extracted_json_response, list):
            for item in extracted_json_response:
                link = item.get("link")
                # Reject if link has spaces (likely a title) or is exceedingly long
                if link and " " not in link.strip() and len(link) < 255:
                     valid_items.append(item)
                else:
                     print(f"Warning: Rejected invalid link from LLM: {link}")
        
        return valid_items

    else:
        response = llm.chat_completion(model=config.huggingface_model_name, 
                                          messages=messages)
        response = response.choices[0].message.content
        extracted_json_response = extract_json_from_output(response)
        return extracted_json_response
    
def secondary_call_to_LLM(url, article_content,llm):
    """
    Performs a secondary call to LLM for detailed extraction from HTML content.

    Args:
        html (str): HTML content of a specific news article.
        llm (object): Instance of the LLM client.

    Returns:
        dict: Extracted detailed JSON response including decision, category, summary, and reasoning.
    """
    messages = [
            {
                "role": "system",
                "content": prompts.prompt_secondary_call_to_llm(url)
            },
            {
                "role": "user",
                "content": f"Article content:\n{article_content}"
            }
        ]
    
    if config.USE_OPENAI:
        response = llm.chat.completions.create(
            model=config.openai_model_name,  # or "gpt-4", or your preferred model
            messages=messages
        )

        response = response.choices[0].message.content
        extracted_json_response = extract_json_from_output(response)

        return extracted_json_response
    
    else:
        response = llm.chat_completion(model=config.huggingface_model_name, 
                                          messages=messages)
        response = response.choices[0].message.content
        extracted_json_response = extract_json_from_output(response)

        return extracted_json_response
    
## --> Fetching the number of tokens from a string   
def num_tokens_from_string(string, encoding_name = config.encoding_name) -> int: 
       """Returns the number of tokens in a text string."""
       encoding = tiktoken.get_encoding(encoding_name)
       num_tokens = len(encoding.encode(string))
       return num_tokens

## --> Checking if the tokens is under limit or not   
def is_within_gpt41_token_limit(article_content, max_input_tokens=config.max_input_token):
    token_count = num_tokens_from_string(article_content)
    print()
    print(f"token count: {token_count}")
    return token_count <= max_input_tokens

def truncate_to_tokens(text: str, max_tokens: int, encoding_name: str = config.encoding_name) -> str:
    """Truncates text to specified number of tokens"""
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)[:max_tokens]
    return encoding.decode(tokens)

def chunk_text(text: str, max_tokens: int, encoding_name: str = config.encoding_name, overlap: int = 200) -> list:
    """Splits text into chunks with token overlap"""
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)
    
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + max_tokens
        chunks.append(encoding.decode(tokens[start:end]))
        start = end - overlap  # 200 token overlap by default
    return chunks

## --> Auto generating token.json so that need not do the sign in again and again
def auto_generated_token_json(
    credentials_path=os.path.join(config.BASE_DIR, 'Inputs', 'credentials.json'),
    token_path=os.path.join(config.BASE_DIR, 'Inputs', 'token.json'),
    scopes=config.SCOPES
):
    """
    Ensures a valid token.json exists for Gmail API authentication.
    - Refreshes token if expired and refresh token is available.
    - Initiates OAuth flow if no valid token is present.
    Returns:
        Credentials object (google.oauth2.credentials.Credentials)
    """
    creds = None

    # Load existing token if present
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)

    # If credentials are expired but refresh token is available, refresh
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    # If no valid credentials, start OAuth flow
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
        creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())

    return creds