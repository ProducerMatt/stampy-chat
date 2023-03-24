import jsonlines
import numpy as np
from typing import List, Dict, Tuple, DefaultDict, Any
from collections import defaultdict
import re
import time
import random
import pickle
import openai
import concurrent.futures
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)  # for exponential backoff
import tiktoken

from pathlib import Path
import config
from text_splitter import TokenSplitter, split_into_sentences
from settings import PATH_TO_DATA, PATH_TO_EMBEDDINGS, PATH_TO_DATASET, EMBEDDING_MODEL, LEN_EMBEDDINGS


openai.api_key = config.OPENAI_API_KEY

error_count_dict = {
    "Entry has no source.": 0,
    "Entry has no title.": 0,
    "Entry has no text.": 0,
    "Entry has no URL.": 0,
    "Entry has wrong citation level.": 0
}


class MissingDataException(Exception):
    pass


class Dataset:
    def __init__(self,
            jsonl_data_path: str,  # Path to the dataset .jsonl file.
            custom_sources: List[str] = None,  # List of sources to include, like "alignment forum", "lesswrong", "arxiv",etc.
            rate_limit_per_minute: int = 3_500,  # Rate limit for the OpenAI API.
            min_tokens_per_block: int = 400, # Minimum number of tokens per block.
            max_tokens_per_block: int = 600, # Maximum number of tokens per block.
            fraction_of_articles_to_use: float = 1.0,  # Fraction of articles to use. If 1.0, use all articles.
        ):
        self.jsonl_data_path = jsonl_data_path
        self.custom_sources = custom_sources
        self.rate_limit_per_minute = rate_limit_per_minute
        self.delay_in_seconds = 60.0 / self.rate_limit_per_minute
        self.fraction_of_articles_to_use = fraction_of_articles_to_use
        
        self.min_tokens_per_block = min_tokens_per_block  # for the text splitter
        self.max_tokens_per_block = max_tokens_per_block  # for the text splitter
        
        self.metadata: List[Tuple[str]] = []  # List of tuples, each containing the title of an article, its URL, and text. E.g.: [('title', 'url', 'text'), ...]
        self.embedding_strings: List[str] = []  # List of strings, each being a few paragraphs from a single article (not exceeding 1000 words).
        
        self.articles_count: DefaultDict[str, int] = defaultdict(int)  # Number of articles per source. E.g.: {'source1': 10, 'source2': 20, 'total': 30}

        if self.custom_sources is not None:
            for source in self.custom_sources:
                self.articles_count[source] = 0
        self.total_articles_count = 0
        
        self.total_char_count = 0
        self.total_word_count = 0
        self.total_sentence_count = 0
        self.total_block_count = 0
        
        self.sources_so_far: List[str] = []
        self.info_types: Dict[str, List[str]] = {}
    
    def extract_info_from_article(self, article: Dict[str, Any]) -> Tuple[str]:
        """
        This function extracts the title, author, date, URL, tags, and text from an article.
        
        Args:
            article (Dict[str, Any]): a dictionary containing the article's text and metadata.

        Returns:
            Tuple[str]: a tuple containing the title, author, date, URL, tags, and text of the article.
        """
        title = None
        author = None
        date_published = None
        url = None
        tags = None
        text = None
        
        # Get title
        if 'title' in article and 'book_title' in article and article['title']: title = article['title']
        elif 'book_title' in article and 'title' not in article and article['book_title']: 
            title = article['book_title']
            if title[-1] == '\n': title = title[:-1]
        elif 'title' in article and article['title']: 
            title = article['title']
            if title[-1] == '\n': title = title[:-1]
        else: title = None

        # Get author
        if 'author' in article and 'authors' in article and article['author']: author = article['author']
        elif 'authors' in article and article['authors']: author = article['authors']
        elif 'author' in article and article['author']: author = article['author']
        else: author = None

        # Get date published
        if 'date_published' in article and article['date_published'] and len(article['date_published']) >= 10: date_published = article['date_published'][:10]
        elif 'published' in article and article['published'] and len(article['published']) >= 16: date_published = article['published'][:16]
        else: date_published = None
            
        # Get URL
        if 'link' in article and article['link']: url = article['link']
        elif 'url' in article and article['url']: url = article['url']
        elif 'doi' in article and article['doi']: url = article['doi']
        else: url = None
            
        # Get tags
        if 'tags' in article and article['tags']:
            if type(article['tags']) == list: tags = ', '.join([val['term'] for val in article['tags']])
            elif type(article['tags']) == str: tags = article['tags']
            else: tags = None
        
        # Get text
        if 'text' in article and article['text']: text = article['text']
        else:
            raise MissingDataException(f"Entry has no text.")

        return (title, author, date_published, url, tags, text)
           
    def get_alignment_texts(self):
        text_splitter = TokenSplitter(self.min_tokens_per_block, self.max_tokens_per_block)
        with jsonlines.open(self.jsonl_data_path, "r") as reader:
            for entry in reader:
                try:
                    if 'source' not in entry: 
                        if 'url' in entry and entry['url'] == "https://www.cold-takes.com/": 
                            entry["source"] = "Cold Takes"
                        elif 'question' in entry and 'answer' in entry: 
                            entry["source"] = "printouts"
                            continue # for now, skip printouts
                        elif 'article_url' in entry and entry['article_url'] == "https://www.gwern.net":
                            entry["source"] = "gwern.net"
                        elif 'url' in entry and entry['url'] == "https://generative.ink/posts/":
                            entry["source"] = "generative.ink"
                        elif 'url' in entry and entry['url'][:24] == "https://greaterwrong.com":
                            entry["source"] = "greaterwrong.com"
                        else:
                            raise MissingDataException("Entry has no source.")
                    
                    random_number = random.random()
                    if random_number > self.fraction_of_articles_to_use:
                        continue
                    
                    # if we specified custom sources, only include articles from those sources
                    if (self.custom_sources is not None) and (entry['source'] not in self.custom_sources):
                        continue
                    self.articles_count[entry['source']] += 1
                    self.total_articles_count += 1
                    
                    # Get title, author, date, URL, tags, and text
                    title, author, date_published, url, tags, text = self.extract_info_from_article(entry)
                                                            
                    # Get signature
                    signature = ""
                    if title: signature += f"Title: {title}, "
                    if author: signature += f"Author: {author}, "
                    if date_published: signature += f"Date published: {date_published}, "
                    if url: signature += f"URL: {url}, "
                    # if tags: signature += f"Tags: {tags}, "  # Temporary decision to not include tags in the signature
                    if signature: signature = signature[:-2]
                    
                    # Add info to metadata and embedding strings
                    self.metadata.append((title, author, date_published, url, tags, text))
                    blocks = text_splitter.split(text, signature)
                    self.embedding_strings.extend(blocks)
                    
                    # Update counts
                    self.total_char_count += len(text)
                    self.total_word_count += len(text.split())
                    self.total_sentence_count += len(split_into_sentences(text))
                    self.total_block_count += len(blocks)
                
                except MissingDataException as e:
                    if str(e) not in error_count_dict:
                        error_count_dict[str(e)] = 0
                    error_count_dict[str(e)] += 1

    def get_embeddings(self):
        # Get an embedding for each text, with retries if necessary
        @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(5))
        def get_embedding_at_index(text: str, i: int, delay_in_seconds: float = 0) -> np.ndarray:
            time.sleep(delay_in_seconds)
            embedding = openai.Embedding.create(
                model=EMBEDDING_MODEL, 
                input=text
            )
            return i, embedding["data"][0]["embedding"]
        
        start = time.time()
        self.embeddings = np.zeros((len(self.embedding_strings), LEN_EMBEDDINGS))
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(get_embedding_at_index, text, i) for i, text in enumerate(self.embedding_strings)]
            num_completed = 0
            for future in concurrent.futures.as_completed(futures):
                i, embedding = future.result()
                self.embeddings[i] = embedding
                num_completed += 1
                if num_completed % 50 == 0:
                    print(f"Completed {num_completed}/{len(self.embedding_strings)} embeddings in {time.time() - start:.2f} seconds.")
        print(f"Completed {num_completed}/{len(self.embedding_strings)} embeddings in {time.time() - start:.2f} seconds.")
    
    def save_embeddings(self, path: str):
        np.save(path, self.embeddings)
        
    def load_embeddings(self, path: str):
        self.embeddings = np.load(path)
        
    def save_class(self, path: str):
        with open(path, 'wb') as f:
            pickle.dump(self, f)

if __name__ == "__main__":
    # List of possible sources:
    all_sources = ["https://aipulse.org", "ebook", "https://qualiacomputing.com", "alignment forum", "lesswrong", "manual", "arxiv", "https://deepmindsafetyresearch.medium.com", "waitbutwhy.com", "GitHub", "https://aiimpacts.org", "arbital.com", "carado.moe", "nonarxiv_papers", "https://vkrakovna.wordpress.com", "https://jsteinhardt.wordpress.com", "audio-transcripts", "https://intelligence.org", "youtube", "reports", "https://aisafety.camp", "curriculum", "https://www.yudkowsky.net", "distill",
                "Cold Takes", "printouts", "gwern.net", "generative.ink", "greaterwrong.com"] # These sources do not have a source field in the .jsonl file

    # List of sources we are using for the test run:
    custom_sources = [
        "https://aipulse.org", 
        "ebook", 
        # "https://qualiacomputing.com", 
        # "alignment forum", 
        # "lesswrong", 
        "manual", 
        # "arxiv", 
        "https://deepmindsafetyresearch.medium.com", 
        "waitbutwhy.com", 
        "GitHub", 
        # "https://aiimpacts.org", 
        # "arbital.com", 
        "carado.moe", 
        # "nonarxiv_papers", 
        "https://vkrakovna.wordpress.com", 
        "https://jsteinhardt.wordpress.com", 
        "audio-transcripts", 
        # "https://intelligence.org", 
        # "youtube", 
        # "reports", 
        "https://aisafety.camp", 
        "curriculum", 
        "https://www.yudkowsky.net", 
        # "distill",
        # "Cold Takes",
        # "printouts",
        # "gwern.net",
        # "generative.ink",
        # "greaterwrong.com"
    ]

    dataset = Dataset(
        jsonl_data_path=PATH_TO_DATA.resolve(), 
        custom_sources=custom_sources, 
        rate_limit_per_minute=3500, 
        min_tokens_per_block=500, max_tokens_per_block=600, 
        # fraction_of_articles_to_use=1/2000
    )
    dataset.get_alignment_texts()
    # dataset.get_embeddings()
    # dataset.save_embeddings("embeddings.npy")
    
    # dataset.save_class("dataset.pkl")
    # dataset = pickle.load(open("dataset.pkl", "rb"))
    
    