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
        logger.warning(f"Translation to English failed: {e}")
    return text


class EmailOperations:
    # ... (rest of class) ...

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

                # Translate subject to English
                email_subject_en = ensure_english(email.get('subject', ''))

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

                        # Translate Title and Body to English
                        title_en = ensure_english(article_content.get("title", ""))
                        body_en = ensure_english(article_content.get("text", ""))

                        article_string = f"Title:\n{title_en}\n\nText:\n{body_en}"
                        
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

                        category = result_dict.get('Category', 'error')
                        summary = result_dict.get('Summary', 'error')
                        decision = result_dict.get('Decision', 'error')
                        reasoning = result_dict.get('Reasoning', 'error')

                        if isinstance(reasoning, list):
                            reasoning = "\n".join(str(item) for item in reasoning)

                        summary_japanese = utils.summary_japanese_translate(summary)
                        category_code = utils.category_code_mapper(category)

                        dedup_dict = {
                            "title": title_en,
                            "text": body_en,
                            "summary": summary
                        }
                        is_duplicate, reason = deduplicator.check_deduplication(dedup_dict)

                        if is_duplicate:
                            logging.info(f"Duplicate article detected, skipping: {dedup_dict['title']}")
                        else:
                            logging.info(f"Unique article detected. Added {dedup_dict['title']} to the database")

                        new_email = Email(
                            company_name=email['company_name'],
                            subject=email_subject_en, # Use translated subject
                            date=email['date'],
                            time=email['time'],
                            link=link,
                            title=result_dict.get('title', title_en), # Use generated or translated title
                            body=body_en, # Use translated body
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
                        logging.info(f"Prepared email insertion: {email_subject_en} from {link}")

                    except Exception as e:
                        logging.error(f"Error processing email '{email.get('subject')}' with URL '{link}': {str(e)}")
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