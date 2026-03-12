import numpy as np
import os
import pickle
from datetime import datetime, timedelta
from huggingface_hub import InferenceClient
from tqdm import tqdm
import sys
# import faiss  # Moved inside class to handle DLL errors
# from sentence_transformers import SentenceTransformer # Moved inside class
 
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

token = os.getenv("HUGGINGFACE_API_KEY")
 
class ArticleDeduplicator:
    def __init__(
        self,
        index_path=None,
        model_name="paraphrase-multilingual-MiniLM-L12-v2"
    ):
        if index_path is None:
            index_path = config.DEDUPLICATION_INDEX_PATH
            
        self.use_api = False
        self.openai_client = None
        self.enabled = True
        self._faiss_lib = None

        # Try importing faiss
        try:
            import faiss
            self._faiss_lib = faiss
        except Exception as e:
            print(f"WARNING: faiss could not be loaded ({e}). Deduplication will be disabled.")
            self.enabled = False
            return

        # CHECK CONFIG FIRST: Use OpenAI if enabled to save memory
        if config.USE_OPENAI and config.OPENAI_API_KEY:
            try:
                from openai import OpenAI
                self.openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
                self.use_api = True
                self.model_name = "text-embedding-3-small"
                print("SUCCESS: Initialized OpenAI API for embeddings (Memory Optimized).")
            except Exception as e:
                 print(f"ERROR: Failed to initialize OpenAI client: {e}")
                 self.enabled = False
                 return
        else:
            # Fallback to local model (Heavy Memory Usage)
            print("WARNING: OpenAI not enabled. Attempting to load local SentenceTransformer (High Memory Usage).")
            try:
                from sentence_transformers import SentenceTransformer
                self.model_name = SentenceTransformer(model_name)
            except Exception as e:
                print(f"ERROR: Local embedding model failed to load ({e}). Deduplication disabled.")
                self.enabled = False

        self.index_path = index_path
        self.vector_store_path = f"{index_path}.vectors.pkl"
        
        # Adjust dimension based on model
        if self.use_api:
            self.embedding_dim = 1536 # text-embedding-3-small dimension
        else:
            self.embedding_dim = 384
            
        self.SIMILARITY_THRESHOLD = 0.8
        self.vector_store = []

        if self.enabled:
            # Check for dimension mismatch if loading existing index
            if os.path.exists(self.index_path):
                try:
                    self.load_index()
                    if self.id_map.d != self.embedding_dim:
                         print(f"Index dimension mismatch (File: {self.id_map.d}, Current: {self.embedding_dim}). Resetting index.")
                         # Reset if using a different model (e.g. switching from local 384 to API 1536)
                         self.index = self._faiss_lib.IndexFlatIP(self.embedding_dim)
                         self.id_map = self._faiss_lib.IndexIDMap(self.index)
                         self.vector_store = []
                except Exception as e:
                    print(f"Error loading index: {e}. Creating new index.")
                    self.index = self._faiss_lib.IndexFlatIP(self.embedding_dim)
                    self.id_map = self._faiss_lib.IndexIDMap(self.index)
            else:
                self.index = self._faiss_lib.IndexFlatIP(self.embedding_dim)
                self.id_map = self._faiss_lib.IndexIDMap(self.index)
                self.vector_store = []
        else:
            self.id_map = None

    def get_embedding(self, text):
        if self.use_api and self.openai_client:
            text = text.replace("\n", " ")
            return np.array(self.openai_client.embeddings.create(input=[text], model=self.model_name).data[0].embedding)
        else:
            emb = self.model_name.encode(text, normalize_embeddings=True)
            return np.array(emb)

    def compute_embedding(self, article):
        if not self.enabled:
            return None
        # Accepts dict with keys: 'title', 'summary', 'text'
        title_emb = self.get_embedding(article['title'])
        summary_emb = self.get_embedding(article['summary'])
        # text_emb = self.get_embedding(article['text']) # Optional: skipping full text for API cost/speed if desired, but keeping logic consistent
        text_emb = self.get_embedding(article['text'][:8191]) # Truncate for API limit just in case

        combined_emb = title_emb + 0.5 * text_emb + 0.3 * summary_emb
        normed_emb = combined_emb / np.linalg.norm(combined_emb)
        return normed_emb.astype('float32')
 
    def check_deduplication(self, article):
        if not self.enabled:
            return False, "Deduplication disabled due to initialization error"
            
        current_time = datetime.now()
        embed = self.compute_embedding(article)
        if embed is None:
            return False, "Embedding failed"
        
        embed = embed.astype('float32')

        self._cleanup_old_vectors(current_time)

        if self.id_map.ntotal == 0:
            self._add_to_index(embed, current_time, article)
            return False, None  # No duplication

        distances, indices = self.id_map.search(embed.reshape(1, -1), 5)

        for i, score in zip(indices[0], distances[0]):
            if score > self.SIMILARITY_THRESHOLD:
                duplicate_article_index = i
                # Fetch the title from vector_store
                duplicate_title = self.vector_store[duplicate_article_index]['title']
                reason = f"Duplicate of article '{duplicate_title}' with similarity {score:.4f}"
                return True, reason

        self._add_to_index(embed, current_time, article)
        return False, None

 
    def _add_to_index(self, embed, timestamp, article):
        vector_id = len(self.vector_store)
        self.id_map.add_with_ids(embed.reshape(1, -1), np.array([vector_id]))
        # Store embedding, timestamp, and title
        self.vector_store.append({
            'embedding': embed,
            'timestamp': timestamp,
            'title': article['title']
        })
 
    def _cleanup_old_vectors(self, current_time):
        cutoff = current_time - timedelta(days=30)
        new_store = []
        valid_ids = []
        for idx, entry in enumerate(self.vector_store):
            if entry['timestamp'] > cutoff:
                new_store.append(entry)
                valid_ids.append(idx)
        self.vector_store = new_store
        self.id_map.reset()
        if valid_ids:
            embeddings = np.vstack([entry['embedding'] for entry in new_store])
            self.id_map.add_with_ids(embeddings, np.array(valid_ids))

 
    def save_index(self):
        if not self.enabled or self.id_map is None:
            return
        self._faiss_lib.write_index(self.id_map, self.index_path)
        with open(self.vector_store_path, 'wb') as f:
            pickle.dump(self.vector_store, f)
 
    def load_index(self):
        if not self.enabled:
            return
        self.id_map = self._faiss_lib.read_index(self.index_path)
        if os.path.exists(self.vector_store_path):
            with open(self.vector_store_path, 'rb') as f:
                self.vector_store = pickle.load(f)
        else:
            self.vector_store = []
 
    def get_all_vectors(self):
        if self.vector_store:
            return np.vstack([emb for emb, _ in self.vector_store])
        return np.array([])
 
if __name__ == "__main__":
    deduplicator = ArticleDeduplicator()
    if deduplicator.enabled:
        print(f"Index contains {deduplicator.id_map.ntotal} vectors")
    else:
        print("Deduplication is disabled.")
 
# articles = []
# urls_to_be_tested = [
#     'https://www.uniphore.com/press-releases/uniphore-recognized-on-2024-deloitte-technology-fast-500-as-one-of-north-americas-fastest-growing-companies/#:~:text=November%2021%2C%202024%20%E2%80%94%20Uniphore%2C,now%20in%20its%2030th%20year.',
#     'https://news.bloombergtax.com/financial-accounting/ex-pwc-partner-blames-tax-scandal-fall-out-on-leaders-failures',
#     'https://www.afr.com/companies/professional-services/pwc-sues-ex-partner-paul-mcnab-over-tax-leaks-scandal-20241120-p5ks7e',
#     'https://www.hrleader.com.au/law/26227-pwc-sues-former-employee-over-tax-leaks-scandal',
#     'https://www.bcg.com/ja-jp/press/20november2024-seventy-percent-economies-underprepared-ai-disruption',
#     'https://www.prnewswire.com/news-releases/seventy-percent-of-economies-are-underprepared-for-ai-disruption-302310674.html',
#     'https://newsroom.accenture.com/news/2024/accenture-expands-generative-ai-powered-cybersecurity-services-and-capabilities-to-accelerate-clients-resilience-and-reinvention',
#     'https://www.cfo.com/news/how-cfos-can-drive-growth-amid-geopolitical-uncertainty/733381/'
# ]
 
# for url in tqdm(urls_to_be_tested):
#     html_content = utils.fetch_using_jina(url)
#     article = utils.deduplication_generation(html_content)
#     articles.append(article)
 
# original_articles = []
# for article in articles:
#     if not deduplicator.check_deduplication(article[0]):
#         original_articles.append(article[0])
 
# print("\nUnique Articles:")
# for article in original_articles:
#     print(f"- {article['title']}")
 
# Load index first (if exists)
# if os.path.exists("faiss_hnsw.index"):
#     index = faiss.read_index("faiss_hnsw.index")
# else:
#     raise FileNotFoundError("Index file not found")
 
# # Get all stored vectors from TemporalArticleDeduplicator's storage
# vectors = np.vstack([emb for emb, _ in deduplicator.vector_store])
# print(vectors)
# if deduplicator.vector_store:
#     vectors = np.vstack([emb for emb, _ in deduplicator.vector_store])
#     print(f"Found {len(vectors)} embeddings:")
#     print(vectors)
# else:
#     print("No vectors stored yet. Process articles first.")