import logging
import re
import textwrap

import chromadb
import gradio as gr
import posthog
import torch
from bs4 import BeautifulSoup
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

from modules import chat, shared

from .download_urls import download_urls

logging.info('Intercepting all calls to posthog :)')
posthog.capture = lambda *args, **kwargs: None

# These parameters are customizable through settings.json
params = {
    'chunk_count': 5,
    'chunk_length': 700,
    'strong_cleanup': False,
    'threads': 4,
}


class Collecter():
    def __init__(self):
        pass

    def add(self, texts: list[str]):
        pass

    def get(self, search_strings: list[str], n_results: int) -> list[str]:
        pass

    def clear(self):
        pass


class Embedder():
    def __init__(self):
        pass

    def embed(self, text: str) -> list[torch.Tensor]:
        pass


class ChromaCollector(Collecter):
    def __init__(self, embedder: Embedder):
        super().__init__()
        self.chroma_client = chromadb.Client(Settings(anonymized_telemetry=False))
        self.embedder = embedder
        self.collection = self.chroma_client.create_collection(name="context", embedding_function=embedder.embed)
        self.ids = []

    def add(self, texts: list[str]):
        self.ids = [f"id{i}" for i in range(len(texts))]
        self.collection.add(documents=texts, ids=self.ids)

    def get(self, search_strings: list[str], n_results: int) -> list[str]:
        n_results = min(len(self.ids), n_results)
        result = self.collection.query(query_texts=search_strings, n_results=n_results, include=['documents'])['documents'][0]
        return result

    def get_ids(self, search_strings: list[str], n_results: int) -> list[str]:
        n_results = min(len(self.ids), n_results)
        result = self.collection.query(query_texts=search_strings, n_results=n_results, include=['documents'])['ids'][0]
        return list(map(lambda x: int(x[2:]), result))

    def clear(self):
        self.collection.delete(ids=self.ids)


class SentenceTransformerEmbedder(Embedder):
    def __init__(self) -> None:
        self.model = SentenceTransformer("sentence-transformers/all-mpnet-base-v2")
        self.embed = self.model.encode


embedder = SentenceTransformerEmbedder()
collector = ChromaCollector(embedder)
chat_collector = ChromaCollector(embedder)
chunk_count = 5


def add_chunks_to_collector(chunks, collector):
    collector.clear()
    collector.add(chunks)


def feed_data_into_collector(corpus, chunk_len):
    global collector

    # Defining variables
    chunk_len = int(chunk_len)
    cumulative = ''

    # Breaking the data into chunks and adding those to the db
    cumulative += "Breaking the input dataset...\n\n"
    yield cumulative
    data_chunks = [corpus[i:i + chunk_len] for i in range(0, len(corpus), chunk_len)]
    cumulative += f"{len(data_chunks)} chunks have been found.\n\nAdding the chunks to the database...\n\n"
    yield cumulative
    add_chunks_to_collector(data_chunks, collector)
    cumulative += "Done."
    yield cumulative


def feed_file_into_collector(file, chunk_len):
    yield 'Reading the input dataset...\n\n'
    text = file.decode('utf-8')
    for i in feed_data_into_collector(text, chunk_len):
        yield i


def feed_url_into_collector(urls, chunk_len, strong_cleanup, threads):
    all_text = ''
    cumulative = ''

    urls = urls.strip().split('\n')
    cumulative += f'Loading {len(urls)} URLs with {threads} threads...\n\n'
    yield cumulative
    for update, contents in download_urls(urls, threads=threads):
        yield cumulative + update

    cumulative += 'Processing the HTML sources...'
    yield cumulative
    for content in contents:
        soup = BeautifulSoup(content, features="html.parser")
        for script in soup(["script", "style"]):
            script.extract()

        strings = soup.stripped_strings
        if strong_cleanup:
            strings = [s for s in strings if re.search("[A-Za-z] ", s)]

        text = '\n'.join([s.strip() for s in strings])
        all_text += text

    for i in feed_data_into_collector(all_text, chunk_len):
        yield i


def apply_settings(_chunk_count):
    global chunk_count
    chunk_count = int(_chunk_count)
    settings_to_display = {
        'chunk_count': chunk_count,
    }
    yield f"The following settings are now active: {str(settings_to_display)}"


def custom_generate_chat_prompt(user_input, state, **kwargs):
    global chat_collector

    if state['mode'] == 'instruct':
        results = collector.get(user_input, n_results=chunk_count)
        additional_context = '\nConsider the excerpts below as additional context:\n\n' + '\n'.join(results)
        user_input += additional_context
    else:

        def make_single_exchange(id_):
            output = ''
            output += f"{state['name1']}: {shared.history['internal'][id_][0]}\n"
            output += f"{state['name2']}: {shared.history['internal'][id_][1]}\n"
            return output

        if len(shared.history['internal']) > chunk_count and user_input != '':
            chunks = []
            hist_size = len(shared.history['internal'])
            for i in range(hist_size-1):
                chunks.append(make_single_exchange(i))

            add_chunks_to_collector(chunks, chat_collector)
            query = '\n'.join(shared.history['internal'][-1] + [user_input])
            try:
                best_ids = chat_collector.get_ids(query, n_results=chunk_count)
                additional_context = '\n'
                for id_ in best_ids:
                    if shared.history['internal'][id_][0] != '<|BEGIN-VISIBLE-CHAT|>':
                        additional_context += make_single_exchange(id_)

                logging.warning(f'Adding the following new context:\n{additional_context}')
                state['context'] = state['context'].strip() + '\n' + additional_context
                state['history'] = [shared.history['internal'][i] for i in range(hist_size) if i not in best_ids]
            except RuntimeError:
                logging.error("Couldn't query the database, moving on...")

    return chat.generate_chat_prompt(user_input, state, **kwargs)


def remove_special_tokens(string):
    for k in ['<|begin-user-input|>', '<|end-user-input|>', '<|injection-point|>']:
        string = string.replace(k, '')

    return string.strip()


def input_modifier(string):
    if shared.is_chat():
        return string

    # Find the user input
    pattern = re.compile(r"<\|begin-user-input\|>(.*?)<\|end-user-input\|>", re.DOTALL)
    match = re.search(pattern, string)
    if match:
        user_input = match.group(1).strip()
    else:
        return remove_special_tokens(string)

    # Get the most similar chunks
    results = collector.get(user_input, n_results=chunk_count)

    # Make the replacements
    string = string.replace('<|begin-user-input|>', '').replace('<|end-user-input|>', '')
    string = string.replace('<|injection-point|>', '\n'.join(results))

    return string


def ui():
    with gr.Accordion("Click for more information...", open=False):
        gr.Markdown(textwrap.dedent("""

        ## About

        This extension takes a dataset as input, breaks it into chunks, and adds the result to a local/offline Chroma database.

        The database is then queried during inference time to get the excerpts that are closest to your input. The idea is to create an arbitrarily large pseudo context.

        The core methodology was developed and contributed by kaiokendev, who is working on improvements to the method in this repository: https://github.com/kaiokendev/superbig

        ## Data input

        Start by entering some data in the interface below and then clicking on "Load data".

        Each time you load some new data, the old chunks are discarded.

        ## Chat mode

        #### Instruct

        On each turn, the chunks will be compared to your current input and the most relevant matches will be appended to the input in the following format:

        ```
        Consider the excerpts below as additional context:
        ...
        ```

        The injection doesn't make it into the chat history. It is only used in the current generation. 

        #### Regular chat

        The chunks from the external data sources are ignored, and the chroma database is built based on the chat history instead. The most relevant past exchanges relative to the present input are added to the context string. This way, the extension acts as a long term memory.

        ## Notebook/default modes

        Your question must be manually specified between `<|begin-user-input|>` and `<|end-user-input|>` tags, and the injection point must be specified with `<|injection-point|>`.

        The special tokens mentioned above (`<|begin-user-input|>`, `<|end-user-input|>`, and `<|injection-point|>`) are removed in the background before the text generation begins.

        Here is an example in Vicuna 1.1 format:

        ```
        A chat between a curious user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user's questions.

        USER:

        <|begin-user-input|>
        What datasets are mentioned in the text below?
        <|end-user-input|>

        <|injection-point|>

        ASSISTANT:
        ```

        ⚠️  For best results, make sure to remove the spaces and new line characters after `ASSISTANT:`.

        *This extension is currently experimental and under development.*

        """))

    with gr.Row():
        with gr.Column(min_width=600):
            with gr.Tab("Text input"):
                data_input = gr.Textbox(lines=20, label='Input data')
                update_data = gr.Button('Load data')

            with gr.Tab("URL input"):
                url_input = gr.Textbox(lines=10, label='Input URLs', info='Enter one or more URLs separated by newline characters.')
                strong_cleanup = gr.Checkbox(value=params['strong_cleanup'], label='Strong cleanup', info='Only keeps html elements that look like long-form text.')
                threads = gr.Number(value=params['threads'], label='Threads', info='The number of threads to use while downloading the URLs.', precision=0)
                update_url = gr.Button('Load data')

            with gr.Tab("File input"):
                file_input = gr.File(label='Input file', type='binary')
                update_file = gr.Button('Load data')

            with gr.Tab("Generation settings"):
                chunk_count = gr.Number(value=params['chunk_count'], label='Chunk count', info='The number of closest-matching chunks to include in the prompt.')
                update_settings = gr.Button('Apply changes')

            chunk_len = gr.Number(value=params['chunk_length'], label='Chunk length', info='In characters, not tokens. This value is used when you click on "Load data".')
        with gr.Column():
            last_updated = gr.Markdown()

    update_data.click(feed_data_into_collector, [data_input, chunk_len], last_updated, show_progress=False)
    update_url.click(feed_url_into_collector, [url_input, chunk_len, strong_cleanup, threads], last_updated, show_progress=False)
    update_file.click(feed_file_into_collector, [file_input, chunk_len], last_updated, show_progress=False)
    update_settings.click(apply_settings, [chunk_count], last_updated, show_progress=False)
