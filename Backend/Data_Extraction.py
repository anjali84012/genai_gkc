import os
import re
import base64
import logging
from langdetect import detect
import asyncio
from mtranslate import translate
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from email.utils import parsedate_tz, mktime_tz
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from tqdm import tqdm
from database import db, app, Email
import utils
import html
import json
from Article_deduplication import ArticleDeduplicator
from langdetect import detect
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config



utils.configure_logging(module_type='Backend', log_filename=config.backend_log_path)
logger = logging.getLogger(__name__)

SCOPES = config.SCOPES

deduplicator = ArticleDeduplicator()

creds = utils.auto_generated_token_json()

def normalize_for_matching(article_content):
    title = article_content.get("title", "")
    try:
        from langdetect import detect
        lang = detect(title)
    except:
        lang = "en"

    if lang == "ja":
        try:
            translated_title = translate(title, "en", "ja")
            return translated_title
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            return title
    else:
        return title


class EmailOperations:
    def authenticate_gmail(self):

        """
        Authenticates the Gmail API using OAuth2 flow or existing credentials.

        Returns:
            googleapiclient.discovery.Resource: Authenticated Gmail service object.
        """

        try:
            logger.info("Starting Gmail authentication.")
            token_json_file = os.path.join('Inputs', 'token.json')
            credentials_json_file = os.path.join('Inputs', 'credentials.json')

            creds = None
            if os.path.exists(token_json_file):
                creds = Credentials.from_authorized_user_file(token_json_file, SCOPES)
                logger.info("Loaded credentials from token.json.")
            else:
                logger.warning("token.json not found. Initiating OAuth flow.")

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("Refreshing access token.")
                    creds.refresh(Request())
                    logger.info("Access token refreshed.")
                else:
                    logger.info("No valid credentials available. Starting OAuth flow.")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_json_file, SCOPES
                    )
                    creds = flow.run_local_server(port=0)
                    logger.info("Obtained new credentials.")

                with open(token_json_file, 'w') as token:
                    token.write(creds.to_json())
                    logger.info("Saved credentials to token.json.")

            logger.info("Gmail authentication successful.")
            return build('gmail', 'v1', credentials=creds)
        except Exception as e:
            logger.error(f"Error authenticating Gmail: {e}", exc_info=True)
            return None

    def get_all_unread_emails(self, service):

        """
        Retrieves and processes all unread emails from Gmail, handling pagination.

        Args:
            service (googleapiclient.discovery.Resource): Authenticated Gmail service.

        Returns:
            tuple: (list of relevant email dicts, list of ALL fetched message IDs)
        """

        try:
            logger.info("Fetching unread emails from Gmail.")
            unread_emails = []
            all_message_ids = []
            page_token = None
            
            while True:
                response = service.users().messages().list(
                    userId='me', 
                    q="is:unread", 
                    pageToken=page_token
                ).execute()
                
                messages = response.get('messages', [])
                
                if not messages:
                    if not all_message_ids: # No messages found at all on first page
                        logger.info("No unread emails found.")
                    break
                    
                logger.info(f"Found {len(messages)} unread emails on this page. Processing...")
                
                # Collect IDs for marking as read later
                page_ids = [msg['id'] for msg in messages]
                all_message_ids.extend(page_ids)

                for message in tqdm(messages, desc="Processing Emails", unit="email"):
                    try:
                        message_id = message['id']
                        email_data = service.users().messages().get(userId='me', id=message_id, format='full').execute()
                        headers = email_data.get('payload', {}).get('headers', [])
                        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject Found")

                        date_str = next((h['value'] for h in headers if h['name'] == 'Date'), None)
                        if date_str:
                            parsed_date = parsedate_tz(date_str)
                            if parsed_date:
                                timestamp = mktime_tz(parsed_date)
                                email_datetime = datetime.fromtimestamp(timestamp)
                                formatted_date = email_datetime.strftime('%d-%m-%y')
                                formatted_time = email_datetime.strftime('%H:%M:%S')
                            else:
                                formatted_date = "Unknown"
                                formatted_time = "Unknown"
                        else:
                            formatted_date = "Unknown"
                            formatted_time = "Unknown"
                        
                        company_name_raw = subject.replace("Google Alert - ", "").strip() if "Google Alert - " in subject else "Unknown Company"

                        # --- Translate title for company matching if Japanese ---
                        normalized_company_name = normalize_for_matching({"title": company_name_raw})

                        # --- Company Filtering ---
                        if normalized_company_name not in config.TARGET_COMPANIES:
                            logger.info(f"Skipping {company_name_raw} (normalized: {normalized_company_name}) - not in target list.")
                            continue

                        company_name = normalized_company_name

                        text_body = self.get_email_body(email_data)
                        links = self.extract_links(text_body)

                        unread_emails.append({
                            'id': message_id,
                            'company_name': company_name,
                            'subject': subject,
                            'body': text_body,
                            'links': links,
                            'date': formatted_date,
                            'time': formatted_time
                        })
                    except Exception as e:
                        logger.error(f"Error processing email {message.get('id', 'Unknown ID')}: {e}", exc_info=True)
                
                page_token = response.get('nextPageToken')
                if not page_token:
                    break

            logger.info(f"Retrieved {len(unread_emails)} relevant emails from {len(all_message_ids)} total unread.")
            return unread_emails, all_message_ids
            
        except Exception as error:
            logger.error(f"An error occurred while fetching unread emails: {error}", exc_info=True)
            return [], []

    def get_email_body(self, message):

        """
        Extracts plain text content from a Gmail message payload.

        Args:
            message (dict): Gmail API message object.

        Returns:
            str: Extracted plain text body or error message.
        """

        try:
            payload = message.get('payload', {})
            text_body = ''
            if 'parts' in payload:
                for part in payload['parts']:
                    if part['mimeType'] == 'text/plain':
                        body_data = part['body'].get('data')
                        if body_data:
                            text_body = base64.urlsafe_b64decode(body_data).decode()
            else:
                body_data = payload.get('body', {}).get('data')
                if body_data:
                    text_body = base64.urlsafe_b64decode(body_data).decode()
                else:
                    text_body = "No body found."
            logger.debug("Extracted email body.")
            return text_body
        except Exception as e:
            logger.error(f"Error extracting body for message: {e}", exc_info=True)
            return "Error reading email body."

    @staticmethod
    def extract_links(text_body):

        """
        Extracts and cleans all HTTP/HTTPS links from the plain text email body.

        Args:
            text_body (str): Plain text email body.

        Returns:
            list: Cleaned list of extracted URLs.
        """

        try:
            # Unescape HTML entities
            text_body = html.unescape(text_body)

            # Match any http(s) link (not just those in < >)
            all_links = re.findall(r'https?://[^\s\'"<>]+', text_body)

            cleaned_links = []
            for link in all_links:
                try:
                    # Handle Google redirector
                    if 'google.com/url' in link:
                        parsed_url = urlparse(link)
                        query_params = parse_qs(parsed_url.query)
                        actual_url = query_params.get('url', [None])[0]
                        if actual_url:
                            link = actual_url

                    # Filter out irrelevant links
                    if not ('google' in link.lower() and 'alerts' in link.lower()):
                        cleaned_links.append(link)
                except Exception as e:
                    logger.warning(f"Error processing link {link}: {e}")

            logger.debug(f"Extracted {len(cleaned_links)} links from email body.")
            return cleaned_links
        except Exception as e:
            logger.error(f"Error extracting links: {e}", exc_info=True)
            return []
        
    def combine_chunk_results(self, chunk_results):
        """
        Combine multiple LLM responses into a single result.
        """
        combined = {
            "Category": chunk_results[-1].get("Category", "error") if chunk_results else "error",
            "Summary": "\n".join([cr.get("Summary", "") for cr in chunk_results if cr]),
            "Decision": chunk_results[-1].get("Decision", "error") if chunk_results else "error",
            "Reasoning": [cr.get("Reasoning", "") for cr in chunk_results if cr]
        }
        return combined
    
    def store_emails_in_db(self, unread_emails, llm):
        """
        Stores unread email data into the database after extracting article information.

        Args:
            unread_emails (list): List of email data dicts from Gmail.
            llm (object): Language model object for processing article content.
        """

        with app.app_context():
            for email in unread_emails:
                logging.info(f"Processing email: {email['subject']} on {email['date']}")

                # existing_links = {e.link for e in Email.query.filter_by(subject=email['subject'], date=email['date']).all()}

                for link in email['links']:
                    # Check globally if link already exists
                    if Email.query.filter_by(link=link).first():
                        logging.info(f"Skipping duplicate link from DB: {link}")
                        continue  # Skip duplicates

                    try:
                        article_content = utils.title_text_generation_from_url(link, llm)
                        if (
                                not article_content or
                                "No Title Found" in article_content.get("title", "") or
                                "No content Found" in article_content.get("text", "")
                            ):
                            logging.warning(f"Invalid content from URL: {link}")
                            continue

                        article_string = f"Title:\n{article_content['title']}\n\nText:\n{article_content['text']}"
                        title = article_content.get("title", "")
                        body = article_content.get("text", "")
                        max_tokens = config.max_input_token

                        # --- Token limit handling with truncation (default) ---
                        if utils.is_within_gpt41_token_limit(article_string, max_input_tokens=max_tokens):
                            response = utils.generate_additional_response(article_string, llm)
                            if not response or not isinstance(response, list) or not isinstance(response[0], dict):
                                logging.warning(f"LLM returned invalid response for {link}")
                                continue
                            result_dict = response[0]
                        else:
                            # --- TRUNCATION STRATEGY ---
                            truncated_text = utils.truncate_to_tokens(article_string, max_tokens)
                            logger.warning(f"Truncated content from {link} to fit {max_tokens} tokens.")
                            response = utils.generate_additional_response(truncated_text, llm)
                            if not response or not isinstance(response, list) or not isinstance(response[0], dict):
                                logging.warning(f"LLM returned invalid response for {link} after truncation")
                                continue
                            result_dict = response[0]

                            # --- CHUNKING STRATEGY (Uncomment to use chunking instead of truncation) ---
                            # overlap = 200
                            # chunks = utils.chunk_text(article_string, max_tokens-100, overlap=overlap)
                            # logger.info(f"Processing {len(chunks)} chunks for {link}.")
                            # chunk_results = []
                            # for chunk in chunks:
                            #     chunk_response = utils.generate_additional_response(chunk, llm)
                            #     if isinstance(chunk_response, list) and isinstance(chunk_response[0], dict):
                            #         chunk_results.append(chunk_response[0])
                            #     else:
                            #         logging.warning("Invalid LLM chunk response format.")
                            # result_dict = self.combine_chunk_results(chunk_results)
                            # ------------------------------------------------------------

                        category = result_dict.get('Category', 'error')
                        summary = result_dict.get('Summary', 'error')
                        decision = result_dict.get('Decision', 'error')
                        reasoning = result_dict.get('Reasoning', 'error')

                        if isinstance(reasoning, list):
                            reasoning = "\n".join(str(item) for item in reasoning)

                        summary_japanese = utils.summary_japanese_translate(summary)
                        category_code = utils.category_code_mapper(category)

                        dedup_dict = {
                            "title": article_content["title"],
                            "text": article_content["text"],
                            "summary": summary
                        }
                        is_duplicate, reason = deduplicator.check_deduplication(dedup_dict)

                        if is_duplicate:
                            logging.info(f"Duplicate article detected, skipping: {dedup_dict['title']}")
                        else:
                            logging.info(f"Unique article detected. Added {dedup_dict['title']} to the database")
                        ## --> For checking the language of the article that is if it is japanese 
                        ## --> then only japanese summary will be generated
                        # if detect(article_content['text']) == 'ja':
                        #     summary = ""
                        #     logger.info("Japanese articles detected generating only japanese summary")

                        new_email = Email(
                            company_name=email['company_name'],
                            subject=email['subject'],
                            date=email['date'],
                            time=email['time'],
                            link=link,
                            # body=json.dumps(article_content),  # convert dictionary to JSON string
                            title=result_dict.get('title', title), # Use generated title if available, else fallback to extracted
                            body=body,
                            category=category,
                            category_code=category_code,
                            summary=summary,
                            summary_japanese=summary_japanese,
                            decision=decision,
                            reasoning=reasoning,
                            disapproved=True if category == "error" else False,
                            duplicated_or_not=is_duplicate,
                            reason_of_duplication=reason if reason is not None else "No duplicacy found",
                        )

                        db.session.add(new_email)
                        logging.info(f"Prepared email insertion: {email['subject']} from {link}")

                    except Exception as e:
                        logging.error(f"Error processing email '{email['subject']}' with URL '{link}': {str(e)}")
                        continue

            try:
                db.session.commit()
                logging.info("All emails committed successfully.")
            except Exception as e:
                db.session.rollback()
                logging.error(f"Commit failed: {e}")
            deduplicator.save_index()
            
    def register_gmail_watch(self, service):
        """
        Registers a watch request for the user's Gmail inbox.
        This enables real-time push notifications via Google Cloud Pub/Sub.
        """
        try:
            topic_name = getattr(config, 'GMAIL_PUBSUB_TOPIC', None)
            if not topic_name:
                logger.warning("GMAIL_PUBSUB_TOPIC not found in config. Skipping watch registration.")
                return None

            request_body = {
                'labelIds': ['INBOX'],
                'topicName': topic_name
            }

            response = service.users().watch(userId='me', body=request_body).execute()
            logger.info(f"Gmail watch registered successfully. Response: {response}")
            return response
        except Exception as e:
            logger.error(f"Error registering Gmail watch: {e}", exc_info=True)
            return None

    def mark_emails_as_read(self, service, email_ids):
        """
        Marks a list of emails as read in Gmail.

        Args:
            service (googleapiclient.discovery.Resource): Authenticated Gmail API client.
            email_ids (list): List of email message IDs to mark as read.
        """
        try:
            for email_id in email_ids:
                try:
                    service.users().messages().modify(
                        userId='me',
                        id=email_id,
                        body={'removeLabelIds': ['UNREAD']}
                    ).execute()
                except Exception as e:
                    logger.warning(f"Error marking email {email_id} as read: {e}")
            logger.info("Emails marked as read.")
        except Exception as error:
            logger.error(f"An error occurred while marking emails as read: {error}", exc_info=True)
            

if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    email_operations = EmailOperations()
    llm = utils.load_llm()

    service = email_operations.authenticate_gmail()

    if service:
        # Register for push notifications
        email_operations.register_gmail_watch(service)
        # Expect tuple return now: (relevant_emails, all_fetched_ids)
        unread_emails, all_message_ids = email_operations.get_all_unread_emails(service)
        
        if unread_emails:
            email_operations.store_emails_in_db(unread_emails, llm)
            
        # Mark ALL fetched emails as read to avoid re-processing or cluttering.
        # Note: If an email failed processing entirely, it will still be marked as read
        # to prevent continuous retries of broken content. Successes are already in DB.
        if all_message_ids:
            email_operations.mark_emails_as_read(service, all_message_ids)
        else:
            logger.info("No unread emails to process.")