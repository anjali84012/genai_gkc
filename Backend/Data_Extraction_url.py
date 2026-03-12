from tqdm import tqdm
from urllib.parse import urlparse, urljoin
try:
    from .database import Email, db, app, urls_to_be_scrapped
    from . import utils
except ImportError:
    from database import Email, db, app, urls_to_be_scrapped
    import utils
import logging
from Article_deduplication import ArticleDeduplicator
from datetime import datetime
from langdetect import detect
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

utils.configure_logging(module_type='Backend', log_filename=config.backend_log_path)
logger = logging.getLogger(__name__)

deduplicator = ArticleDeduplicator()

def format_date(date_string, input_format='%Y-%m-%d', output_format='%d-%m-%y'):
    from datetime import datetime
    try:
        return datetime.strptime(date_string, input_format).strftime(output_format)
    except Exception as e:
        logger.warning(f"Could not format date '{date_string}': {e}")
        return date_string  # fallback to original
    
class WebScraper:
    """
    WebScraper class handles scraping articles from a list of URLs, extracting structured
    information using LLMs, and storing them into a database.
    """
    def __init__(self, llm):
        self.llm = llm

    def get_base_url(self, url):
        """Extracts the base URL from a full URL."""
        try:
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            logger.debug(f"Extracted base URL: {base_url}")
            return base_url
        except Exception as e:
            logger.error(f"Error extracting base URL from {url}: {e}")
            return url  # fallback to original URL

    def combine_chunk_results(self, chunk_results):
        """Combine multiple LLM responses into a single result."""
        # This is a simple example; customize as needed for your use case
        combined = {
            "summary": "\n".join([cr.get("summary", "") for cr in chunk_results if cr]),
            "decision": chunk_results[-1]["decision"] if chunk_results else "",
            "reasoning": [cr.get("reasoning", "") for cr in chunk_results if cr]
        }
        return combined
    def insert_scraped_articles_to_db(self, articles):
        """
        Inserts a list of scraped article dictionaries into the Email table.

        Args:
            articles (list): List of dictionaries containing article metadata and content.
        """
        with app.app_context():
            for article in articles:
                try:
                    # Defensive: skip if required fields are missing
                    if not article or not article.get("subject") or not article.get("link"):
                        logger.warning("Skipped article with missing subject or link.")
                        continue

                    # Check for duplicates based on link to avoid re-inserting
                    existing = Email.query.filter_by(link=article["link"]).first()
                    if existing:
                        logger.info(f"Skipped duplicate article in DB: {article['subject']} {article['link']}")
                        continue

                    reasoning = article.get("reasoning")
                    if isinstance(reasoning, list):
                        reasoning = "\n".join(str(item) for item in reasoning)

                    # Prepare fields
                    company_name = article.get("company_name", "Unknown")
                    subject = article.get("subject", "No Subject")
                    link = article.get("link", "")
                    title = article.get("title", "No Title")
                    body = article.get("body", "")
                    
                    def get_case_insensitive_key(d, target_key, default_val):
                        for k, v in d.items():
                            if k.lower() == target_key.lower():
                                return v
                        return default_val

                    category = get_case_insensitive_key(article, "category", "Uncategorized")
                    summary = get_case_insensitive_key(article, "summary", "")
                    decision = get_case_insensitive_key(article, "decision", "")
                    date = article.get("date", "")
                    time = article.get("time", "")
                    summary_japanese = utils.summary_japanese_translate(summary)
                    category_code = utils.category_code_mapper(category)
                    
                    # --- Company Filtering ---
                    normalized_company = company_name.strip().lower()
                    normalized_targets = {tc.strip().lower().replace('"', '') for tc in config.TARGET_COMPANIES} # remove quotes from config for cleaner matching
                    
                    # Also add the raw logic just in case
                    normalized_targets.update({tc.strip().lower() for tc in config.TARGET_COMPANIES})

                    # Check if exact match OR if the company name starts with a target (e.g. "EY UK")
                    match_found = False
                    if normalized_company in normalized_targets:
                        match_found = True
                    else:
                        # Fallback: Check if valid target is in the company name (careful with short names)
                        for target in normalized_targets:
                            # If target is long enough, simple containment is safe. 
                            # If short (<= 3 chars like EY, NRI), require word boundary or startswith
                            if len(target) > 3:
                                if target in normalized_company:
                                    match_found = True
                                    logger.info(f"Fuzzy match: '{company_name}' matched target '{target}'")
                                    # Normalize the company name to the target for consistency
                                    company_name = target.upper() if len(target) <=3 else target.title() 
                                    break
                            else:
                                # For short targets, use strict word boundary or exact match (already checked)
                                # or startswith with space
                                if normalized_company.startswith(target + " ") or normalized_company == target:
                                     match_found = True
                                     logger.info(f"Short match: '{company_name}' matched target '{target}'")
                                     company_name = target.upper()
                                     break
                    
                    if not match_found:
                        logger.warning(f"Skipping article '{title}' - Company '{company_name}' (norm: '{normalized_company}') not in target list.")
                        # Log the targets for debugging if needed (once)
                        # logger.debug(f"Targets: {normalized_targets}")
                        continue


                    new_email = Email(
                        company_name=company_name,
                        subject=subject,
                        date=date,
                        time=time,
                        link=link,
                        title=title,
                        body=body,
                        category=category,
                        category_code=category_code,
                        summary=summary,
                        summary_japanese=summary_japanese,
                        decision=decision,
                        reasoning=reasoning,
                        duplicated_or_not=article.get("duplicated_or_not", False),
                        reason_of_duplication=article.get("reason_of_duplication"),
                    )

                    db.session.add(new_email)
                    logger.info(f"Prepared to insert article: {subject} ({link})")
                    
                    # Commit immediately to release lock
                    try:
                        db.session.commit()
                        logger.info(f"Successfully committed article: {title}")
                        # deduplicator.save_index() # Optional: save index less frequently or here
                    except Exception as e:
                        db.session.rollback()
                        logger.error(f"DB commit failed for article '{title}': {e}")

                except Exception as e:
                    logger.error(f"Error preparing article for DB: {e}")
                    db.session.rollback()
                    continue

        try:
            deduplicator.save_index()
            logger.info("FAISS index saved successfully.")
        except Exception as e:
            logger.error(f"Error saving FAISS index: {e}")

    def scrapping_pipeline(self):
        """
        Main scraping pipeline to fetch articles and generate metadata using LLM.

        Returns:
            list: List of extracted article dictionaries.
        """
        final_results = []

        for url in tqdm(urls_to_be_scrapped, desc="Scraping URLs"):
            logger.info(f"Processing URL: {url}")
            try:
                # print(f"DEBUG: Processing URL: {url}")
                html_content = utils.get_article_html(url)
                # print(f"DEBUG: Retrieved HTML content length: {len(html_content) if html_content else 0}")
            except Exception as e:
                logger.error(f"Error fetching HTML for {url}: {e}")
                continue


            try:
                extracted_items = utils.initial_call_to_LLM(html_content, self.llm)
                logger.info(f"Extracted items for {url}: {len(extracted_items) if extracted_items else 0}")
            except Exception as e:
                logger.error(f"Failed to parse LLM initial content from {url}: {e}")
                continue

            if not extracted_items:
                continue

            for item in extracted_items:
                try:
                    link = item.get("link")
                    if not link:
                         link = url 
                    
                    # Handle relative URLs
                    base_url = self.get_base_url(url)
                    full_link = urljoin(base_url, link)
                    logger.debug(f"Full article link resolved: {full_link}")


                    # 1. Fetch Article Content
                    article_content = utils.title_text_generation_from_url(full_link, self.llm)
                    
                    if (not article_content or 
                        "No Title Found" in article_content.get("title", "") or 
                        "No content Found" in article_content.get("text", "")):
                        logging.warning(f"Invalid content from URL: {full_link}")
                        continue

                    title = article_content.get("title", "No Title")
                    body = article_content.get("text", "")
                    
                    # 2. Prepare for Secondary LLM
                    article_string = f"Title:\n{title}\n\nText:\n{body}"
                    max_tokens = config.max_input_token

                    # Token Handling
                    if utils.is_within_gpt41_token_limit(article_string, max_input_tokens=max_tokens):
                        refined_data = utils.secondary_call_to_LLM(full_link, article_string, self.llm)
                    else:
                        truncated_text = utils.truncate_to_tokens(article_string, max_tokens)
                        logger.warning(f"Truncated content from {full_link} to fit {max_tokens} tokens.")
                        refined_data = utils.secondary_call_to_LLM(full_link, truncated_text, self.llm)

                    if not refined_data:
                        logger.warning(f"Secondary LLM failed for {full_link}")
                        continue

                    # 3. Format Date
                    refined_data["date"] = datetime.today().strftime("%d-%m-%y")
                    refined_data["time"] = datetime.now().strftime("%H:%M:%S")

                    # 4. Deduplication Check
                    summary_extracted = get_case_insensitive_key(refined_data, "summary", "")
                    dedup_dict = {
                        "title": title,
                        "text": body,
                        "summary": summary_extracted
                    }
                    is_duplicate, reason = deduplicator.check_deduplication(dedup_dict)
                    
                    if reason is None:
                        reason = "No duplicacy found"

                    refined_data["duplicated_or_not"] = is_duplicate
                    refined_data["reason_of_duplication"] = reason
                    
                    if not is_duplicate:
                        logger.info(f"Unique article: {title}")
                    else:
                        logger.info(f"Duplicate article: {title}")

                    # 5. Construct Final Article Object
                    # Merge all gathered data into one dictionary
                    final_article = {
                        **item,           # Initial extraction data (company_name, etc.)
                        **refined_data,   # Secondary LLM data (category, summary, decision, etc.)
                        "title": title,
                        "body": body,
                        "link": full_link,
                        "subject": title # Use title as subject if not present
                    }
                    
                    # Ensure company_name exists if not in item
                    if "company_name" not in final_article:
                         # fallback or extraction logic if needed, 
                         # usually item has company_name from initial_call_to_LLM
                         final_article["company_name"] = "Unknown"

                    final_results.append(final_article)
                    logger.info(f"Successfully processed article: {title}")

                except Exception as e:
                    logger.error(f"Error processing item from {url}: {e}")
                    continue

        # deduplicator.save_index() moved to insert_scraped_articles_to_db after commit
        return final_results


if __name__ == "__main__":
    try:
        with app.app_context():
            db.create_all()
            logger.info("Database tables created or already exist.")

        llm = utils.load_llm()
        website_scraper = WebScraper(llm)
        logger.info("WebScraper class instantiated.")

        # Updated pipeline call - returns single list of dicts
        final_results = website_scraper.scrapping_pipeline()
        
        # Insert into DB
        website_scraper.insert_scraped_articles_to_db(final_results)
        
        logger.info("Web scraping pipeline completed successfully.")
    except Exception as e:
        logger.critical(f"Fatal error in main execution: {e}", exc_info=True)
        sys.exit(1)
